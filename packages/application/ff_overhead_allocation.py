"""Audited cost-only overhead allocation for the canonical FF ledger."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_business_projection import (
    ensure_warehouse_projection_source_outbox,
)
from packages.application.warehouse_functional import ensure_warehouse_functional_schema


CONTRACT_NAME = "ff_overhead_allocation_v1"
OPERATION_OVERHEAD = "ff_overhead_allocation"
OPERATION_REVERSAL = "ff_overhead_reversal"
SOURCE_TYPE = "ff_overhead"
ZERO = Decimal("0")
RUB_QUANTUM = Decimal("0.01")


class FfOverheadAllocationError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class FfOverheadAllocation:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _now
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            ensure_ff_overhead_schema(conn)
            ensure_warehouse_projection_source_outbox(conn)
            conn.commit()

    def build_plan(
        self,
        *,
        business_date: str,
        amount_rub: Any,
        reason: str,
    ) -> dict[str, Any]:
        normalized_date = _iso_date(business_date)
        amount = _positive_rub(amount_rub)
        normalized_reason = _reason(reason)
        if not normalized_reason:
            raise FfOverheadAllocationError(
                "reason_required",
                "Основание накладных расходов обязательно",
            )
        idempotency_key = "sha256:" + _digest(
            {
                "business_date": normalized_date,
                "amount_rub": _text(amount),
                "reason": normalized_reason,
            }
        )
        with _connect(self.runtime.db_path, query_only=True) as conn:
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return _existing_plan(existing)
            state = _allocation_state(conn, business_date=normalized_date)
        balances = dict(state["balances"])
        denominator = sum((value for value in balances.values() if value > ZERO), ZERO)
        if denominator <= ZERO:
            raise FfOverheadAllocationError(
                "positive_denominator_missing",
                "На выбранную дату нет положительного физического остатка FF",
            )
        allocations = _allocate_rub(amount, balances)
        for allocation in allocations:
            identity = dict(state["nomenclature"].get(int(allocation["nm_id"])) or {})
            allocation.update(
                {
                    "barcode": str(identity.get("barcode") or ""),
                    "sku": str(identity.get("sku") or identity.get("our_sku") or allocation["nm_id"]),
                    "nomenclature_name": str(identity.get("name") or identity.get("nomenclature_name") or ""),
                }
            )
        source_revision = "sha256:" + _digest(
            {
                "business_date": normalized_date,
                "physical_rows": state["physical_rows"],
                "cost_only_rows": state["cost_only_rows"],
                "nomenclature": state["nomenclature"],
                "active_version": state["active_version"],
            }
        )
        source_fingerprint = "sha256:" + _digest(
            {
                "source_revision": source_revision,
                "denominator_quantity": _text(denominator),
                "allocations": allocations,
            }
        )
        document_id = "ffoh_" + idempotency_key.removeprefix("sha256:")[:24]
        operation_id = "ffso_oh_" + idempotency_key.removeprefix("sha256:")[:20]
        manifest = {
            "document_id": document_id,
            "operation_id": operation_id,
            "business_date": normalized_date,
            "amount_rub": _text(amount),
            "reason": normalized_reason,
            "idempotency_key": idempotency_key,
            "source_revision": source_revision,
            "source_fingerprint": source_fingerprint,
            "active_version": state["active_version"],
            "denominator_quantity": _text(denominator),
            "allocations": allocations,
            "affected_nm_ids": [
                int(item["nm_id"])
                for item in allocations
                if _decimal(item["allocation_rub"]) != ZERO
            ],
            "non_target_digest": state["non_target_digest"],
            "invariants": {
                "physical_quantity_delta": "0",
                "allocation_total_rub": _text(
                    sum((_decimal(item["allocation_rub"]) for item in allocations), ZERO)
                ),
                "positive_physical_only": True,
                "reservations_excluded": True,
            },
        }
        fingerprint = "sha256:" + _digest(manifest)
        return {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "apply_allowed": True,
            "idempotent": False,
            "fingerprint": fingerprint,
            "manifest": manifest,
        }

    def apply_plan(
        self,
        *,
        business_date: str,
        amount_rub: Any,
        reason: str,
        confirmation_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        plan = self.build_plan(
            business_date=business_date,
            amount_rub=amount_rub,
            reason=reason,
        )
        if plan.get("idempotent"):
            if confirmation_fingerprint != str(plan.get("fingerprint") or ""):
                raise FfOverheadAllocationError(
                    "stale_or_invalid_fingerprint",
                    "Fingerprint не относится к уже проведённому документу",
                )
            return {**plan, "status": str(plan.get("status") or "already_applied")}
        if confirmation_fingerprint != str(plan["fingerprint"]):
            raise FfOverheadAllocationError(
                "stale_or_invalid_fingerprint",
                "Состояние FF изменилось после preview; выполните проверку ещё раз",
            )
        manifest = dict(plan["manifest"])
        now = str(self.timestamp_factory())
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            ensure_ff_overhead_schema(conn)
            ensure_warehouse_projection_source_outbox(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents WHERE idempotency_key=?",
                    (manifest["idempotency_key"],),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    return _existing_plan(existing)
                fresh_state = _allocation_state(
                    conn,
                    business_date=str(manifest["business_date"]),
                )
                fresh_revision = "sha256:" + _digest(
                    {
                        "business_date": manifest["business_date"],
                        "physical_rows": fresh_state["physical_rows"],
                        "cost_only_rows": fresh_state["cost_only_rows"],
                        "nomenclature": fresh_state["nomenclature"],
                        "active_version": fresh_state["active_version"],
                    }
                )
                if fresh_revision != str(manifest["source_revision"]):
                    raise FfOverheadAllocationError(
                        "source_revision_changed",
                        "Источник FF изменился после preview",
                    )
                if fresh_state["non_target_digest"] != str(manifest["non_target_digest"]):
                    raise FfOverheadAllocationError(
                        "non_target_state_changed",
                        "Связанное состояние FF изменилось после preview",
                    )
                lines = _operation_lines(manifest, reversal=False)
                _insert_cost_only_operation(
                    conn,
                    operation_id=str(manifest["operation_id"]),
                    operation_type=OPERATION_OVERHEAD,
                    source_key=f"ff_overhead:{manifest['idempotency_key']}",
                    source_object_id=str(manifest["document_id"]),
                    source_object_label=(
                        "Распределение накладных расходов FF "
                        + str(manifest["business_date"])
                    ),
                    business_date=str(manifest["business_date"]),
                    created_at=now,
                    created_by=created_by,
                    diagnostics={
                        "effect": "cost_only",
                        "reason": "overhead",
                        "reason_text": str(manifest["reason"]),
                        "amount_rub": str(manifest["amount_rub"]),
                        "source_revision": str(manifest["source_revision"]),
                        "source_fingerprint": str(manifest["source_fingerprint"]),
                        "denominator_quantity": str(manifest["denominator_quantity"]),
                    },
                    lines=lines,
                )
                readback = _cost_only_readback(
                    conn,
                    operation_id=str(manifest["operation_id"]),
                    expected_amount=_decimal(manifest["amount_rub"]),
                    business_date=str(manifest["business_date"]),
                    expected_physical_rows=fresh_state["physical_rows"],
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_ff_overhead_documents(
                        document_id,idempotency_key,business_date,amount_rub,reason,
                        plan_fingerprint,source_revision,source_fingerprint,
                        denominator_quantity,allocations_json,created_by,created_at,
                        status,operation_id,reversal_operation_id,readback_json,
                        non_target_digest
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        manifest["document_id"],
                        manifest["idempotency_key"],
                        manifest["business_date"],
                        manifest["amount_rub"],
                        manifest["reason"],
                        confirmation_fingerprint,
                        manifest["source_revision"],
                        manifest["source_fingerprint"],
                        manifest["denominator_quantity"],
                        _json(manifest["allocations"]),
                        str(created_by or "operator"),
                        now,
                        "applied",
                        manifest["operation_id"],
                        "",
                        _json(readback),
                        manifest["non_target_digest"],
                    ),
                )
                _enqueue_replay(
                    conn,
                    document_id=str(manifest["document_id"]),
                    source_revision=str(manifest["source_fingerprint"]),
                    business_date=str(manifest["business_date"]),
                    nm_ids=manifest["affected_nm_ids"],
                    requested_at=now,
                    reversal=False,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "contract_name": CONTRACT_NAME,
            "status": "applied",
            "idempotent": False,
            "document_id": manifest["document_id"],
            "operation_id": manifest["operation_id"],
            "fingerprint": confirmation_fingerprint,
            "readback": readback,
            "replay": {"status": "queued", "business_date": manifest["business_date"]},
        }

    def build_reversal_plan(self, *, document_id: str, reason: str) -> dict[str, Any]:
        normalized_reason = _reason(reason)
        if not normalized_reason:
            raise FfOverheadAllocationError(
                "reversal_reason_required",
                "Причина сторно обязательна",
            )
        with _connect(self.runtime.db_path, query_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents WHERE document_id=?",
                (str(document_id),),
            ).fetchone()
            if row is None:
                raise FfOverheadAllocationError(
                    "document_not_found",
                    "Документ накладных расходов не найден",
                )
            if str(row["status"]) == "reversed":
                return {
                    "contract_name": CONTRACT_NAME,
                    "status": "already_reversed",
                    "idempotent": True,
                    "fingerprint": str(row["plan_fingerprint"]),
                    "document_id": str(row["document_id"]),
                    "reversal_operation_id": str(row["reversal_operation_id"]),
                }
            if str(row["status"]) != "applied":
                raise FfOverheadAllocationError(
                    "document_state_invalid",
                    "Сторно доступно только для проведённого документа",
                )
            allocations = list(_loads(row["allocations_json"], []))
            physical_rows = _physical_rows(conn, business_date=str(row["business_date"]))
            source_revision = "sha256:" + _digest(
                {
                    "document_id": str(row["document_id"]),
                    "operation_id": str(row["operation_id"]),
                    "allocations": allocations,
                    "physical_rows": physical_rows,
                }
            )
        reversal_id = "ffso_oh_rb_" + hashlib.sha256(
            str(row["document_id"]).encode("utf-8")
        ).hexdigest()[:18]
        manifest = {
            "document_id": str(row["document_id"]),
            "original_operation_id": str(row["operation_id"]),
            "reversal_operation_id": reversal_id,
            "business_date": str(row["business_date"]),
            "amount_rub": str(row["amount_rub"]),
            "reason": normalized_reason,
            "allocations": allocations,
            "source_revision": source_revision,
            "physical_rows": physical_rows,
        }
        return {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "apply_allowed": True,
            "idempotent": False,
            "fingerprint": "sha256:" + _digest(manifest),
            "manifest": manifest,
        }

    def apply_reversal(
        self,
        *,
        document_id: str,
        reason: str,
        confirmation_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        plan = self.build_reversal_plan(document_id=document_id, reason=reason)
        if plan.get("idempotent"):
            return plan
        if confirmation_fingerprint != str(plan["fingerprint"]):
            raise FfOverheadAllocationError(
                "stale_or_invalid_fingerprint",
                "Состояние документа изменилось после preview сторно",
            )
        manifest = dict(plan["manifest"])
        now = str(self.timestamp_factory())
        with _connect(self.runtime.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_overhead_documents WHERE document_id=?",
                    (manifest["document_id"],),
                ).fetchone()
                if row is None or str(row["status"]) != "applied":
                    raise FfOverheadAllocationError(
                        "document_state_changed",
                        "Документ уже изменён после preview сторно",
                    )
                fresh_physical = _physical_rows(
                    conn,
                    business_date=str(manifest["business_date"]),
                )
                fresh_revision = "sha256:" + _digest(
                    {
                        "document_id": str(row["document_id"]),
                        "operation_id": str(row["operation_id"]),
                        "allocations": list(_loads(row["allocations_json"], [])),
                        "physical_rows": fresh_physical,
                    }
                )
                if fresh_revision != str(manifest["source_revision"]):
                    raise FfOverheadAllocationError(
                        "reversal_source_changed",
                        "Физическая история FF изменилась после preview сторно",
                    )
                reversal_manifest = {
                    "document_id": manifest["document_id"],
                    "business_date": manifest["business_date"],
                    "amount_rub": manifest["amount_rub"],
                    "reason": manifest["reason"],
                    "source_revision": manifest["source_revision"],
                    "denominator_quantity": str(row["denominator_quantity"]),
                    "allocations": manifest["allocations"],
                }
                _insert_cost_only_operation(
                    conn,
                    operation_id=str(manifest["reversal_operation_id"]),
                    operation_type=OPERATION_REVERSAL,
                    source_key=f"ff_overhead_reversal:{manifest['document_id']}",
                    source_object_id=str(manifest["document_id"]),
                    source_object_label=(
                        "Сторно накладных расходов FF " + str(manifest["business_date"])
                    ),
                    business_date=str(manifest["business_date"]),
                    created_at=now,
                    created_by=created_by,
                    diagnostics={
                        "effect": "cost_only",
                        "reason": "correction",
                        "reason_text": str(manifest["reason"]),
                        "original_operation_id": str(manifest["original_operation_id"]),
                        "exact_original_allocation": True,
                    },
                    lines=_operation_lines(reversal_manifest, reversal=True),
                )
                readback = _cost_only_readback(
                    conn,
                    operation_id=str(manifest["reversal_operation_id"]),
                    expected_amount=-_decimal(manifest["amount_rub"]),
                    business_date=str(manifest["business_date"]),
                    expected_physical_rows=fresh_physical,
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_ff_overhead_documents
                    SET status='reversed',reversal_operation_id=?,readback_json=?
                    WHERE document_id=? AND status='applied'
                    """,
                    (
                        manifest["reversal_operation_id"],
                        _json({**_loads(row["readback_json"], {}), "reversal": readback}),
                        manifest["document_id"],
                    ),
                )
                _enqueue_replay(
                    conn,
                    document_id=str(manifest["document_id"]),
                    source_revision=str(manifest["source_revision"]),
                    business_date=str(manifest["business_date"]),
                    nm_ids=[
                        int(item["nm_id"])
                        for item in manifest["allocations"]
                        if _decimal(item["allocation_rub"]) != ZERO
                    ],
                    requested_at=now,
                    reversal=True,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "contract_name": CONTRACT_NAME,
            "status": "reversed",
            "idempotent": False,
            "document_id": manifest["document_id"],
            "reversal_operation_id": manifest["reversal_operation_id"],
            "fingerprint": confirmation_fingerprint,
            "readback": readback,
            "replay": {"status": "queued", "business_date": manifest["business_date"]},
        }


def ensure_ff_overhead_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_overhead_documents(
            document_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            business_date TEXT NOT NULL,
            amount_rub TEXT NOT NULL,
            reason TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            denominator_quantity TEXT NOT NULL,
            allocations_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_id TEXT NOT NULL UNIQUE,
            reversal_operation_id TEXT NOT NULL,
            readback_json TEXT NOT NULL,
            non_target_digest TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ff_overhead_documents_by_business_date
        ON sheet_vitrina_v1_ff_overhead_documents(business_date DESC,created_at DESC)
        """
    )


def _allocation_state(conn: sqlite3.Connection, *, business_date: str) -> dict[str, Any]:
    physical_rows = _physical_rows(conn, business_date=business_date)
    balances: dict[int, Decimal] = {}
    for row in physical_rows:
        nm_id = int(row["nm_id"])
        balances[nm_id] = balances.get(nm_id, ZERO) + _decimal(row["quantity_delta"])
    balances = {nm_id: quantity for nm_id, quantity in balances.items() if quantity > ZERO}
    if not balances:
        raise FfOverheadAllocationError(
            "positive_denominator_missing",
            "На выбранную дату нет положительного физического остатка FF",
        )
    placeholders = ",".join("?" for _ in balances)
    rows = conn.execute(
        f"""
        SELECT item_id,nm_id,our_sku,nomenclature_name,barcode,
               product_type AS group_name,updated_at
        FROM sheet_vitrina_v1_nomenclature_items
        WHERE nm_id IN ({placeholders}) AND is_active=1 AND is_hidden=0
        ORDER BY nm_id,item_id
        """,
        tuple(sorted(balances)),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["nm_id"]), []).append(dict(row))
    ambiguous = [
        {"nm_id": nm_id, "active_candidate_count": len(grouped.get(nm_id, []))}
        for nm_id in sorted(balances)
        if len(grouped.get(nm_id, [])) != 1
    ]
    if ambiguous:
        raise FfOverheadAllocationError(
            "nomenclature_unmatched_or_ambiguous",
            "Положительный остаток содержит неизвестный или неоднозначный SKU",
            details=ambiguous,
        )
    active = conn.execute(
        """
        SELECT version.version_id,version.business_effective_date,
               version.plan_fingerprint,version.published_at
        FROM sheet_vitrina_v1_warehouse_functional_active active
        JOIN sheet_vitrina_v1_warehouse_functional_versions version
          ON version.version_id=active.version_id WHERE active.slot=1
        """
    ).fetchone()
    _assert_unambiguous_chronology(conn, business_date=business_date)
    return {
        "physical_rows": physical_rows,
        "cost_only_rows": _cost_only_rows(conn, business_date=business_date),
        "balances": balances,
        "nomenclature": {nm_id: grouped[nm_id][0] for nm_id in sorted(balances)},
        "active_version": dict(active) if active is not None else {},
        "non_target_digest": _non_target_digest(conn),
    }


def _cost_only_rows(conn: sqlite3.Connection, *, business_date: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT operation.operation_id,operation.business_effective_date,
                   operation.created_at,operation.operation_type,line.line_no,line.nm_id,
                   line.raw_json
            FROM sheet_vitrina_v1_ff_stock_operations operation
            JOIN sheet_vitrina_v1_ff_stock_operation_lines line
              ON line.operation_id=operation.operation_id
            WHERE operation.operation_type IN (?,?)
              AND COALESCE(NULLIF(operation.business_effective_date,''),
                           substr(operation.created_at,1,10))<=?
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """,
            (OPERATION_OVERHEAD, OPERATION_REVERSAL, business_date),
        ).fetchall()
    ]


def _physical_rows(conn: sqlite3.Connection, *, business_date: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT operation.operation_id,operation.business_effective_date,
                   operation.created_at,operation.operation_type,operation.source_type,
                   line.line_no,line.nm_id,CAST(line.quantity_delta AS TEXT) AS quantity_delta
            FROM sheet_vitrina_v1_ff_stock_operations operation
            JOIN sheet_vitrina_v1_ff_stock_operation_lines line
              ON line.operation_id=operation.operation_id
            WHERE COALESCE(NULLIF(operation.business_effective_date,''),
                           substr(operation.created_at,1,10))<=?
              AND operation.operation_type NOT IN (?,?)
              AND ABS(line.quantity_delta)>0.000000001
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """,
            (business_date, OPERATION_OVERHEAD, OPERATION_REVERSAL),
        ).fetchall()
    ]


def _assert_unambiguous_chronology(conn: sqlite3.Connection, *, business_date: str) -> None:
    rows = conn.execute(
        """
        SELECT operation_id,COALESCE(NULLIF(business_effective_date,''),substr(created_at,1,10)) AS d
        FROM sheet_vitrina_v1_ff_stock_operations
        WHERE operation_type NOT IN (?,?)
        ORDER BY created_at,operation_id
        """,
        (OPERATION_OVERHEAD, OPERATION_REVERSAL),
    ).fetchall()
    later_seen = False
    ambiguous: list[str] = []
    for row in rows:
        if str(row["d"] or "") > business_date:
            later_seen = True
        elif later_seen:
            ambiguous.append(str(row["operation_id"]))
    if ambiguous:
        raise FfOverheadAllocationError(
            "business_chronology_ambiguous",
            "История FF содержит поздно загруженные операции вокруг выбранной даты",
            details={"operation_ids": ambiguous[:20], "business_date": business_date},
        )


def _allocate_rub(amount: Decimal, balances: Mapping[int, Decimal]) -> list[dict[str, Any]]:
    positive = [(int(nm_id), quantity) for nm_id, quantity in balances.items() if quantity > ZERO]
    denominator = sum((quantity for _, quantity in positive), ZERO)
    raw = {
        nm_id: amount * quantity / denominator for nm_id, quantity in positive
    }
    allocations = {
        nm_id: value.quantize(RUB_QUANTUM, rounding=ROUND_DOWN)
        for nm_id, value in raw.items()
    }
    remainder = amount - sum(allocations.values(), ZERO)
    cents = int((remainder / RUB_QUANTUM).to_integral_value())
    order = sorted(
        positive,
        key=lambda item: (-(raw[item[0]] - allocations[item[0]]), item[0]),
    )
    for index in range(cents):
        allocations[order[index % len(order)][0]] += RUB_QUANTUM
    rows = [
        {
            "nm_id": nm_id,
            "physical_quantity": _text(quantity),
            "allocation_rub": _text(allocations[nm_id]),
            "allocation_per_unit_rub": _text(allocations[nm_id] / quantity),
        }
        for nm_id, quantity in sorted(positive)
    ]
    if sum((_decimal(item["allocation_rub"]) for item in rows), ZERO) != amount:
        raise FfOverheadAllocationError(
            "allocation_does_not_conserve",
            "Decimal allocation does not conserve the document amount",
        )
    return rows


def _operation_lines(manifest: Mapping[str, Any], *, reversal: bool) -> list[dict[str, Any]]:
    sign = Decimal("-1") if reversal else Decimal("1")
    return [
        {
            "nm_id": int(item["nm_id"]),
            "barcode": str(item.get("barcode") or ""),
            "sku": str(item.get("sku") or item["nm_id"]),
            "nomenclature_name": str(item.get("nomenclature_name") or ""),
            "quantity_delta": "0",
            "raw": {
                "cost_adjustment": {
                    "capital_delta_rub": _text(sign * _decimal(item["allocation_rub"])),
                    "allocation_basis_quantity": str(item["physical_quantity"]),
                    "allocation_per_unit_rub": str(item["allocation_per_unit_rub"]),
                    "document_id": str(manifest["document_id"]),
                    "business_date": str(manifest["business_date"]),
                    "reason": str(manifest["reason"]),
                    "source_revision": str(manifest["source_revision"]),
                    "exact_original_allocation_reversal": bool(reversal),
                }
            },
        }
        for item in manifest["allocations"]
        if _decimal(item["allocation_rub"]) != ZERO
    ]


def _insert_cost_only_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    operation_type: str,
    source_key: str,
    source_object_id: str,
    source_object_label: str,
    business_date: str,
    created_at: str,
    created_by: str,
    diagnostics: Mapping[str, Any],
    lines: Iterable[Mapping[str, Any]],
) -> None:
    normalized_lines = [dict(item) for item in lines]
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
            operation_type,
            SOURCE_TYPE,
            source_key,
            source_object_id,
            source_object_label,
            created_at,
            business_date,
            str(created_by or "operator"),
            len(normalized_lines),
            0,
            0,
            "[]",
            _json(dict(diagnostics)),
            "",
            "",
            "",
            None,
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
                "",
                "",
                0,
                _json(item["raw"]),
            )
            for index, item in enumerate(normalized_lines, start=1)
        ],
    )
    conn.execute(
        """
        UPDATE sheet_vitrina_v1_warehouse_business_projection_outbox
        SET source_kind='ff_stock_cost_only_overhead'
        WHERE stable_source_id LIKE ? AND status='queued'
        """,
        (f"ff_operation:{operation_id}:%",),
    )


def _enqueue_replay(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    source_revision: str,
    business_date: str,
    nm_ids: Iterable[int],
    requested_at: str,
    reversal: bool,
) -> None:
    stable_source_id = (
        "ff_overhead_reversal:" if reversal else "ff_overhead:"
    ) + document_id
    queue_id = "whrq_" + _digest(
        {"stable_source_id": stable_source_id, "source_revision": source_revision}
    )[:24]
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue(
            queue_id,stable_source_id,source_revision,effective_date,
            affected_nm_ids_json,status,requested_at,started_at,finished_at,error
        ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)
        ON CONFLICT(queue_id) DO UPDATE SET
            effective_date=MIN(effective_date,excluded.effective_date),
            affected_nm_ids_json=excluded.affected_nm_ids_json,
            status=CASE WHEN status='complete' THEN status ELSE 'queued' END,
            requested_at=excluded.requested_at,error=NULL
        """,
        (
            queue_id,
            stable_source_id,
            source_revision,
            business_date,
            _json(sorted({int(item) for item in nm_ids})),
            requested_at,
        ),
    )


def _cost_only_readback(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    expected_amount: Decimal,
    business_date: str,
    expected_physical_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT quantity_delta,raw_json FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
        (operation_id,),
    ).fetchall()
    amount = sum(
        (
            _decimal(
                (dict(_loads(row["raw_json"], {})).get("cost_adjustment") or {}).get(
                    "capital_delta_rub"
                )
            )
            for row in rows
        ),
        ZERO,
    )
    physical_unchanged = all(_decimal(row["quantity_delta"]) == ZERO for row in rows)
    actual_physical_rows = _physical_rows(
        conn,
        business_date=business_date,
    )
    expected_rows = [dict(item) for item in expected_physical_rows]
    physical_unchanged = physical_unchanged and _json(actual_physical_rows) == _json(expected_rows)
    return {
        "allocation_total_rub": _text(amount),
        "expected_amount_rub": _text(expected_amount),
        "allocation_conserves": amount == expected_amount,
        "physical_quantity_unchanged": physical_unchanged,
        "line_count": len(rows),
        "physical_source_row_count": len(actual_physical_rows),
    }


def _non_target_digest(conn: sqlite3.Connection) -> str:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    payload: dict[str, Any] = {}
    for table, order in (
        ("sheet_vitrina_v1_ff_stock_reservation_operations", "operation_id"),
        ("sheet_vitrina_v1_ff_stock_reservation_lines", "operation_id,line_no"),
        ("sheet_vitrina_v1_ff_stock_wb_supply_lifecycle", "supply_id"),
    ):
        if table in tables:
            payload[table] = [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            ]
    return "sha256:" + _digest(payload)


def _existing_plan(row: Mapping[str, Any]) -> dict[str, Any]:
    status = "already_applied" if str(row["status"]) == "applied" else "already_reversed"
    return {
        "contract_name": CONTRACT_NAME,
        "status": status,
        "apply_allowed": False,
        "idempotent": True,
        "fingerprint": str(row["plan_fingerprint"]),
        "document_id": str(row["document_id"]),
        "operation_id": str(row["operation_id"]),
        "reversal_operation_id": str(row["reversal_operation_id"]),
        "readback": _loads(row["readback_json"], {}),
    }


def _positive_rub(value: Any) -> Decimal:
    amount = _decimal(value)
    try:
        normalized = amount.quantize(RUB_QUANTUM)
    except InvalidOperation as exc:
        raise FfOverheadAllocationError(
            "invalid_amount",
            "Сумма должна быть конечным положительным числом",
        ) from exc
    if not amount.is_finite() or amount <= ZERO or amount != normalized:
        raise FfOverheadAllocationError(
            "invalid_amount",
            "Сумма должна быть положительной и содержать не более двух знаков после запятой",
        )
    return amount


def _reason(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) > 500:
        raise FfOverheadAllocationError(
            "reason_too_long",
            "Основание ограничено 500 символами",
        )
    return normalized


def _iso_date(value: Any) -> str:
    normalized = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise FfOverheadAllocationError(
            "invalid_business_date",
            "Business date должна иметь формат YYYY-MM-DD",
        ) from exc


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FfOverheadAllocationError("invalid_decimal", "Некорректное число") from exc


def _text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(path: Path, *, query_only: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if query_only else str(path)
    conn = sqlite3.connect(uri, uri=query_only, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    return conn
