"""Persisted event contour for management invested product capital.

The contour is intentionally independent from 1C.  It tracks paid ownership/cost
layers and physical stage movements with Decimal-compatible SQLite text values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_STAGES,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
    own_stage_metric_key,
)
from packages.contracts.supplier_financial_documents import (
    FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR,
    FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
)


ZERO = Decimal("0")
ONE = Decimal("1")
MONEY_QUANT = Decimal("0.000001")
QTY_QUANT = Decimal("0.000001")

STAGE_PRODUCTION = "PRODUCTION"
STAGE_PRODUCTION_TO_FF = "PRODUCTION_TO_FF"
STAGE_FF = "FF"
STAGE_FF_TO_WB = "FF_TO_WB"
STAGE_WB = "WB"

EVENT_SUPPLIER_PAYMENT = "supplier_payment"
EVENT_COST_PAYMENT = "cost_payment"
EVENT_STAGE_TRANSFER = "stage_transfer"
EVENT_WB_ACCEPTANCE = "wb_acceptance"
EVENT_WB_RECONCILIATION = "wb_reconciliation"

TARGETED_ORPHAN_DOPRINATO_SUPPLY_ID = "40654176"
TARGETED_ORPHAN_DOPRINATO_EFFECTIVE_DATE = "2026-07-06"
TARGETED_ORPHAN_DOPRINATO_NM_ID = 391663632
TARGETED_ORPHAN_DOPRINATO_QUANTITY = Decimal("1")
TARGETED_ORPHAN_DOPRINATO_WAREHOUSE = "Склад Шушары"
TARGETED_ORPHAN_DOPRINATO_FIFO_SUPPLY_ID = "40433285"
TARGETED_ORPHAN_DOPRINATO_REASON = "historical_orphan_doprinato_zero_capital"
TARGETED_ORPHAN_DOPRINATO_EVENT_ID = (
    "wb_reconciliation:40654176:historical_orphan:391663632"
)
TARGETED_ORPHAN_DOPRINATO_NORMAL_EVENT_ID = (
    "wb_reconciliation:40654176:40433285:391660889:1"
)
TARGETED_ORPHAN_DOPRINATO_DOCUMENT_QUANTITIES = {
    391660889: Decimal("1"),
    391663632: Decimal("1"),
}


@dataclass(frozen=True)
class OwnProductCapitalRebuildResult:
    event_count: int
    date_count: int
    daily_rows_changed: int
    blocker_count: int
    fingerprint: str


class OwnProductCapitalBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            _ensure_own_capital_schema(conn)

    def has_supplier_payment_layer(self, payment_id: str) -> bool:
        """Return whether a durable capital layer already depends on a payment document."""
        payment_id = _required_text(payment_id, "payment_id")
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            return conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id = ?",
                (payment_id,),
            ).fetchone() is not None

    def has_cost_payment_event(self, document_id: str) -> bool:
        document_id = _required_text(document_id, "document_id")
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            return (
                conn.execute(
                    """
                    SELECT 1 FROM sheet_vitrina_v1_own_capital_events
                    WHERE event_type = ? AND event_id LIKE ? ESCAPE '\\' LIMIT 1
                    """,
                    (EVENT_COST_PAYMENT, _literal_like_prefix(f"cost_payment:{document_id}:")),
                ).fetchone()
                is not None
            )

    def record_supplier_payment(
        self,
        *,
        payment_id: str,
        shipment_id: str,
        effective_date: str,
        invoice_total_cny: Any,
        paid_cny: Any,
        paid_rub: Any,
        product_lines: Iterable[Mapping[str, Any]],
        actual_shipment_date: str | None = None,
        actual_ff_acceptance_date: str | None = None,
        expenses_complete: bool = False,
        provenance: Mapping[str, Any] | None = None,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        payment_id = _required_text(payment_id, "payment_id")
        shipment_id = _required_text(shipment_id, "shipment_id")
        effective_date = _iso_date(effective_date, "effective_date")
        invoice_total = _positive_decimal(invoice_total_cny, "invoice_total_cny")
        payment_cny = _positive_decimal(paid_cny, "paid_cny")
        payment_rub = _positive_decimal(paid_rub, "paid_rub")
        lines = _validated_product_lines(product_lines)
        stage = _physical_stage_for_supplier_payment(
            effective_date=effective_date,
            actual_shipment_date=actual_shipment_date,
            actual_ff_acceptance_date=actual_ff_acceptance_date,
        )
        fingerprint_payload = {
            "payment_id": payment_id,
            "shipment_id": shipment_id,
            "effective_date": effective_date,
            "invoice_total_cny": _text_decimal(invoice_total),
            "paid_cny": _text_decimal(payment_cny),
            "paid_rub": _text_decimal(payment_rub),
            "lines": lines,
            "provenance": dict(provenance or {}),
        }
        fingerprint = _stable_hash(fingerprint_payload)
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            _ensure_own_capital_schema(conn)
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["fingerprint"]) != fingerprint:
                    self._record_blocker(
                        code="payment_id_conflict",
                        source_identity=payment_id,
                        details={"shipment_id": shipment_id},
                    )
                    raise ValueError("payment_id already exists with different financial evidence")
                return {
                    "status": "ok",
                    "idempotent": True,
                    "payment_id": payment_id,
                    "incremental_paid_share": str(existing["incremental_paid_share"]),
                    "cumulative_paid_share": str(existing["cumulative_paid_share"]),
                    "stage": str(existing["stage"]),
                }
            cumulative_rows = conn.execute(
                """
                SELECT paid_cny
                FROM sheet_vitrina_v1_own_capital_payment_layers
                WHERE shipment_id = ?
                """,
                (shipment_id,),
            ).fetchall()
            cumulative_before = sum((_decimal(row["paid_cny"]) for row in cumulative_rows), ZERO)
            cumulative_after = cumulative_before + payment_cny
            if cumulative_after < ZERO or cumulative_after > invoice_total:
                self._record_blocker(
                    code="supplier_payment_overpayment",
                    source_identity=payment_id,
                    details={
                        "invoice_total_cny": _text_decimal(invoice_total),
                        "cumulative_paid_cny": _text_decimal(cumulative_after),
                    },
                )
                raise ValueError("supplier payment exceeds invoice_total_cny; allocation failed closed")
            incremental_share = payment_cny / invoice_total
            cumulative_share = cumulative_after / invoice_total
            if cumulative_share > ONE:
                raise ValueError("invalid cumulative paid share")
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_own_capital_payment_layers (
                    payment_id, shipment_id, effective_date, invoice_total_cny, paid_cny,
                    paid_rub, incremental_paid_share, cumulative_paid_share, stage,
                    expenses_complete, provenance_json, fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    shipment_id,
                    effective_date,
                    _text_decimal(invoice_total),
                    _text_decimal(payment_cny),
                    _text_decimal(payment_rub),
                    _text_decimal(incremental_share),
                    _text_decimal(cumulative_share),
                    stage,
                    1 if expenses_complete else 0,
                    _json_dumps(dict(provenance or {})),
                    fingerprint,
                    now,
                ),
            )
            allocated = _allocate_payment(lines, paid_share=incremental_share, paid_rub=payment_rub)
            for index, line in enumerate(allocated, start=1):
                self._insert_event(
                    conn,
                    event_id=f"supplier_payment:{payment_id}:{line['nm_id']}:{index}",
                    event_type=EVENT_SUPPLIER_PAYMENT,
                    effective_date=effective_date,
                    shipment_id=shipment_id,
                    supply_id="",
                    nm_id=int(line["nm_id"]),
                    stage_from="",
                    stage_to=stage,
                    quantity=line["paid_equivalent_qty"],
                    capital_rub=line["allocated_rub"],
                    confirmed_quantity=line["paid_equivalent_qty"],
                    cost_layer_id=f"supplier:{payment_id}:{line['nm_id']}:{index}",
                    warehouse="",
                    destination="",
                    payload={
                        "payment_id": payment_id,
                        "supplier_line_id": line["line_id"],
                        "incremental_paid_share": _text_decimal(incremental_share),
                        "cumulative_paid_share": _text_decimal(cumulative_share),
                        "expenses_complete": bool(expenses_complete),
                        "confirmation_reason": "paid_purchase_cost",
                    },
                    created_at=now,
                )
            conn.commit()
        if recalculate:
            self.recalculate()
        return {
            "status": "ok",
            "idempotent": False,
            "payment_id": payment_id,
            "incremental_paid_share": _text_decimal(incremental_share),
            "cumulative_paid_share": _text_decimal(cumulative_share),
            "stage": stage,
            "allocations": allocated,
        }

    def has_events(self) -> bool:
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            return conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_own_capital_events LIMIT 1"
            ).fetchone() is not None

    def first_paid_ownership_date(self) -> str | None:
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            row = conn.execute(
                """
                SELECT MIN(effective_date) AS effective_date
                FROM sheet_vitrina_v1_own_capital_events
                WHERE event_type = ? AND quantity != '0'
                """,
                (EVENT_SUPPLIER_PAYMENT,),
            ).fetchone()
        value = str(row["effective_date"] or "") if row is not None else ""
        return value or None

    def plan_historical_wb_paid_scope(
        self,
        *,
        supply_id: str,
        effective_date: str,
        sent_quantities_by_nm: Mapping[int, Any],
        accepted_quantities_by_nm: Mapping[int, Any],
    ) -> dict[str, Any]:
        """Bound a historical physical WB movement to capital actually owned on FF."""
        supply_id = _required_text(supply_id, "supply_id")
        effective_date = _iso_date(effective_date, "effective_date")
        sent = {
            _positive_int(nm_id, "nm_id"): _positive_decimal(qty, f"sent[{nm_id}]")
            for nm_id, qty in sent_quantities_by_nm.items()
        }
        accepted = {
            _positive_int(nm_id, "nm_id"): _nonnegative_decimal(
                qty, f"accepted[{nm_id}]"
            )
            for nm_id, qty in accepted_quantities_by_nm.items()
        }
        existing_layers = self._wb_supply_layers(supply_id)
        state = self._state_as_of(effective_date)
        tracked_sent: dict[int, Decimal] = {}
        tracked_accepted: dict[int, Decimal] = {}
        diagnostics: list[dict[str, Any]] = []
        for nm_id, physical_sent in sent.items():
            existing = existing_layers.get(nm_id)
            if existing is not None:
                owned_sent = existing["quantity"]
                if owned_sent > physical_sent:
                    raise ValueError(
                        f"persisted WB capital quantity exceeds current sent quantity for nmID {nm_id}"
                    )
            else:
                available = state.get(nm_id, {}).get(STAGE_FF, _empty_bucket())["qty"]
                owned_sent = min(physical_sent, max(available, ZERO))
            if owned_sent <= ZERO:
                diagnostics.append(
                    {
                        "nm_id": nm_id,
                        "code": "physical_wb_quantity_without_paid_ff_capital",
                        "physical_sent": _text_decimal(physical_sent),
                        "tracked_sent": "0",
                    }
                )
                continue
            tracked_sent[nm_id] = owned_sent
            if owned_sent < physical_sent:
                diagnostics.append(
                    {
                        "nm_id": nm_id,
                        "code": "physical_wb_quantity_partially_outside_paid_capital",
                        "physical_sent": _text_decimal(physical_sent),
                        "tracked_sent": _text_decimal(owned_sent),
                    }
                )
            if nm_id not in accepted:
                continue
            physical_accepted = accepted[nm_id]
            owned_accepted = min(physical_accepted, owned_sent)
            tracked_accepted[nm_id] = owned_accepted
            if physical_accepted > owned_sent:
                diagnostics.append(
                    {
                        "nm_id": nm_id,
                        "code": "physical_accepted_quantity_partially_outside_paid_capital",
                        "physical_accepted": _text_decimal(physical_accepted),
                        "tracked_accepted": _text_decimal(owned_accepted),
                    }
                )
            if physical_accepted > physical_sent:
                diagnostics.append(
                    {
                        "nm_id": nm_id,
                        "code": "physical_accepted_quantity_exceeds_sent_layer",
                        "physical_sent": _text_decimal(physical_sent),
                        "physical_accepted": _text_decimal(physical_accepted),
                        "tracked_accepted": _text_decimal(owned_accepted),
                    }
                )
        return {
            "sent_quantities_by_nm": tracked_sent,
            "accepted_quantities_by_nm": tracked_accepted,
            "diagnostics": diagnostics,
        }

    def matching_wb_outstanding_quantities(
        self,
        *,
        effective_date: str,
        quantities_by_nm: Mapping[int, Any],
        warehouse: str,
        destination: str,
        original_supply_id: str | None = None,
    ) -> dict[str, Any]:
        """Return eligible tracked outstanding quantities without mutating reconciliation state."""
        effective_date = _iso_date(effective_date, "effective_date")
        requested_nm_ids = {
            _positive_int(nm_id, "nm_id") for nm_id in quantities_by_nm
        }
        result = {nm_id: ZERO for nm_id in requested_nm_ids}
        physical_result = {nm_id: ZERO for nm_id in requested_nm_ids}
        candidate_nm_ids: set[int] = set()
        candidate_diagnostics: list[dict[str, Any]] = []
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            for nm_id in requested_nm_ids:
                params: list[Any] = [nm_id, effective_date]
                where = ["nm_id = ?", "final_acceptance_date <= ?"]
                if original_supply_id:
                    where.append("original_supply_id = ?")
                    params.append(str(original_supply_id))
                else:
                    where.extend(["warehouse = ?", "destination = ?"])
                    params.extend([str(warehouse or ""), str(destination or "")])
                rows = conn.execute(
                    f"""
                    SELECT original_supply_id, nm_id, final_acceptance_date,
                           open_quantity, physical_open_quantity
                    FROM sheet_vitrina_v1_own_capital_wb_outstanding
                    WHERE {' AND '.join(where)}
                    ORDER BY final_acceptance_date, original_supply_id
                    """,
                    tuple(params),
                ).fetchall()
                if rows:
                    candidate_nm_ids.add(nm_id)
                result[nm_id] = sum(
                    (max(_decimal(row["open_quantity"]), ZERO) for row in rows),
                    ZERO,
                )
                physical_result[nm_id] = sum(
                    (
                        max(_decimal(row["physical_open_quantity"]), ZERO)
                        for row in rows
                    ),
                    ZERO,
                )
                candidate_diagnostics.extend(
                    {
                        "nm_id": int(row["nm_id"]),
                        "original_supply_id": str(row["original_supply_id"]),
                        "final_acceptance_date": str(row["final_acceptance_date"]),
                        "tracked_open_quantity": _text_decimal(
                            max(_decimal(row["open_quantity"]), ZERO)
                        ),
                        "physical_open_quantity": _text_decimal(
                            max(_decimal(row["physical_open_quantity"]), ZERO)
                        ),
                    }
                    for row in rows
                )
        return {
            "tracked_available_by_nm": {
                str(nm_id): _text_decimal(quantity)
                for nm_id, quantity in result.items()
            },
            "physical_available_by_nm": {
                str(nm_id): _text_decimal(quantity)
                for nm_id, quantity in physical_result.items()
            },
            "candidate_nm_ids": sorted(candidate_nm_ids),
            "candidates": candidate_diagnostics,
        }

    def resolve_blockers(self, *, source_identity: str, codes: Iterable[str] | None = None) -> int:
        normalized_identity = _required_text(source_identity, "source_identity")
        normalized_codes = [str(item).strip() for item in (codes or []) if str(item).strip()]
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            if normalized_codes:
                placeholders = ",".join("?" for _ in normalized_codes)
                cursor = conn.execute(
                    f"""
                    UPDATE sheet_vitrina_v1_own_capital_blockers
                    SET resolved_at = ?
                    WHERE source_identity = ? AND resolved_at IS NULL
                      AND code IN ({placeholders})
                    """,
                    (now, normalized_identity, *normalized_codes),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_own_capital_blockers
                    SET resolved_at = ?
                    WHERE source_identity = ? AND resolved_at IS NULL
                    """,
                    (now, normalized_identity),
                )
            conn.commit()
            return int(cursor.rowcount or 0)

    def record_cost_payment(
        self,
        *,
        document_id: str,
        shipment_id: str,
        effective_date: str,
        allocations: Iterable[Mapping[str, Any]],
        stage: str,
        expenses_complete: bool,
        component: str,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        document_id = _required_text(document_id, "document_id")
        shipment_id = _required_text(shipment_id, "shipment_id")
        effective_date = _iso_date(effective_date, "effective_date")
        stage = _stage(stage)
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(allocations, start=1):
            nm_id = _positive_int(raw.get("nm_id"), f"allocations[{index}].nm_id")
            amount = _positive_decimal(raw.get("capital_rub"), f"allocations[{index}].capital_rub")
            normalized.append(
                {
                    "nm_id": nm_id,
                    "capital_rub": amount,
                    "affected_quantity": _nonnegative_decimal(
                        raw.get("affected_quantity") or 0,
                        f"allocations[{index}].affected_quantity",
                    ),
                }
            )
        if not normalized:
            raise ValueError("cost payment requires deterministic SKU allocations")
        now = self.timestamp_factory()
        fingerprint = _stable_hash(
            {
                "document_id": document_id,
                "shipment_id": shipment_id,
                "effective_date": effective_date,
                "stage": stage,
                "component": component,
                "allocations": [
                    {
                        "nm_id": item["nm_id"],
                        "capital_rub": _text_decimal(item["capital_rub"]),
                        "affected_quantity": _text_decimal(item["affected_quantity"]),
                    }
                    for item in normalized
                ],
                "provenance": dict(provenance or {}),
            }
        )
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            existing = conn.execute(
                """
                SELECT evidence_hash FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id LIKE ? ESCAPE '\\' LIMIT 1
                """,
                (_literal_like_prefix(f"cost_payment:{document_id}:"),),
            ).fetchone()
            if existing is not None:
                if str(existing["evidence_hash"]) != fingerprint:
                    raise ValueError("cost payment document already materialized with different allocations")
                return {"status": "ok", "idempotent": True, "document_id": document_id}
            for index, item in enumerate(normalized, start=1):
                self._insert_event(
                    conn,
                    event_id=f"cost_payment:{document_id}:{item['nm_id']}:{index}",
                    event_type=EVENT_COST_PAYMENT,
                    effective_date=effective_date,
                    shipment_id=shipment_id,
                    supply_id="",
                    nm_id=item["nm_id"],
                    stage_from="",
                    stage_to=stage,
                    quantity=ZERO,
                    capital_rub=item["capital_rub"],
                    confirmed_quantity=ZERO,
                    cost_layer_id=f"expense:{document_id}:{item['nm_id']}:{index}",
                    warehouse="",
                    destination="",
                    payload={
                        "component": str(component or "other"),
                        "expenses_complete": bool(expenses_complete),
                        "invalidate_confirmation": not bool(expenses_complete),
                        "affected_quantity": _text_decimal(item["affected_quantity"]),
                        "confirmation_reason": (
                            "expense_completeness_certified"
                            if expenses_complete
                            else "expense_completeness_not_certified"
                        ),
                        "provenance": dict(provenance or {}),
                    },
                    created_at=now,
                    evidence_hash=fingerprint,
                )
            conn.commit()
        return {"status": "ok", "idempotent": False, "document_id": document_id}

    def record_order_level_cost_payment(
        self,
        *,
        document_id: str,
        shipment_id: str,
        effective_date: str,
        capital_rub: Any,
        product_lines: Iterable[Mapping[str, Any]],
        component: str,
        actual_shipment_date: str | None = None,
        actual_ff_acceptance_date: str | None = None,
        expenses_complete: bool = False,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        amount = _positive_decimal(capital_rub, "capital_rub")
        lines = _validated_product_lines(product_lines)
        total_value = sum((_decimal(line["invoice_value_cny"]) for line in lines), ZERO)
        remaining = amount
        allocations: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            allocated = (
                remaining
                if index == len(lines)
                else amount * _decimal(line["invoice_value_cny"]) / total_value
            )
            remaining -= allocated
            allocations.append(
                {
                    "nm_id": line["nm_id"],
                    "capital_rub": allocated,
                    "affected_quantity": _decimal(line["qty"]),
                }
            )
        return self.record_cost_payment(
            document_id=document_id,
            shipment_id=shipment_id,
            effective_date=effective_date,
            allocations=allocations,
            stage=_physical_stage_for_supplier_payment(
                effective_date=_iso_date(effective_date, "effective_date"),
                actual_shipment_date=actual_shipment_date,
                actual_ff_acceptance_date=actual_ff_acceptance_date,
            ),
            expenses_complete=expenses_complete,
            component=component,
            provenance={
                "allocation_method": "product_invoice_value_proportional",
                **dict(provenance or {}),
            },
        )

    def materialize_persisted_expense_events(
        self,
        *,
        shipment_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        """Create dated capital events from persisted factual expense evidence."""
        requested_shipment = str(shipment_id or "").strip()
        documents = [
            dict(item)
            for item in self.runtime.list_supplier_financial_documents_all()
            if not requested_shipment
            or str(item.get("supplier_order_id") or "") == requested_shipment
        ]
        created = 0
        idempotent = 0
        skipped_cny_ledger_only = 0
        blockers: list[dict[str, Any]] = []
        for document in documents:
            document_type = str(document.get("document_type") or "")
            parse_status = str(document.get("parse_status") or "")
            if parse_status in {
                FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED,
                FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR,
            }:
                continue
            if document_type not in {
                FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
                FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
                FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT,
            }:
                continue
            if (
                document_type == FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT
                and parse_status != FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED
            ):
                continue
            if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW:
                blockers.append(
                    {
                        "code": "expense_document_needs_review",
                        "document_id": str(document.get("document_id") or ""),
                        "document_type": document_type,
                    }
                )
                continue
            current_shipment_id = str(document.get("supplier_order_id") or "").strip()
            detail = (
                self.runtime.load_supplier_shipment(current_shipment_id)
                if current_shipment_id
                else None
            )
            if detail is None:
                blockers.append(
                    {
                        "code": "expense_shipment_missing",
                        "document_id": str(document.get("document_id") or ""),
                        "shipment_id": current_shipment_id,
                    }
                )
                continue
            header = dict(detail.get("header") or {})
            product_lines = [
                {
                    "line_id": line.get("line_id"),
                    "nm_id": line.get("internal_nm_id"),
                    "qty": line.get("qty"),
                    "unit_price": line.get("unit_price"),
                    "amount": line.get("amount"),
                    "match_status": line.get("match_status"),
                }
                for line in detail.get("lines") or []
                if str(line.get("line_type") or "") == "product"
            ]
            expense_lines = [
                dict(item)
                for item in self.runtime.list_supplier_financial_expense_lines(
                    current_shipment_id
                )
                if str(item.get("financial_document_id") or "")
                == str(document.get("document_id") or "")
            ]
            available_plans = _expense_event_plans(document, expense_lines)
            if not available_plans:
                if (
                    document_type == FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT
                    and not _direct_rub_bank_fee_lines(expense_lines)
                ):
                    skipped_cny_ledger_only += 1
                    continue
                blockers.append(
                    {
                        "code": "expense_effective_amount_missing",
                        "document_id": str(document.get("document_id") or ""),
                        "document_type": document_type,
                    }
                )
                continue
            planned = [
                item
                for item in available_plans
                if (not date_from or str(item["effective_date"]) >= date_from)
                and (not date_to or str(item["effective_date"]) <= date_to)
            ]
            if not planned:
                continue
            for plan in planned:
                try:
                    result = self.record_order_level_cost_payment(
                        document_id=str(plan["event_document_id"]),
                        shipment_id=current_shipment_id,
                        effective_date=str(plan["effective_date"]),
                        capital_rub=plan["capital_rub"],
                        product_lines=product_lines,
                        component=str(plan["component"]),
                        actual_shipment_date=(
                            str(header.get("actual_shipment_date") or "") or None
                        ),
                        actual_ff_acceptance_date=(
                            str(header.get("actual_ff_acceptance_date") or "")
                            or None
                        ),
                        expenses_complete=bool(header.get("expenses_complete")),
                        provenance={
                            "source": "supplier_financial_document",
                            "financial_document_id": str(
                                document.get("document_id") or ""
                            ),
                            "document_type": document_type,
                            "parse_status": parse_status,
                            "effective_date_source": str(
                                plan["effective_date_source"]
                            ),
                            "expense_line_ids": list(plan["expense_line_ids"]),
                            "dedupe_key": str(plan["event_document_id"]),
                        },
                    )
                    if bool(result.get("idempotent")):
                        idempotent += 1
                    else:
                        created += 1
                except ValueError as exc:
                    blockers.append(
                        {
                            "code": "expense_capital_allocation_blocked",
                            "document_id": str(document.get("document_id") or ""),
                            "reason": str(exc),
                        }
                    )
        if recalculate and created:
            self.recalculate()
        return {
            "status": "blocked" if blockers else "ok",
            "created_event_group_count": created,
            "idempotent_event_group_count": idempotent,
            "skipped_cny_ledger_only_document_count": skipped_cny_ledger_only,
            "blocker_count": len(blockers),
            "blockers": blockers,
        }

    def set_expenses_certification(
        self,
        *,
        shipment_id: str,
        expenses_complete: bool,
        actor: str = "",
    ) -> dict[str, Any]:
        shipment_id = _required_text(shipment_id, "shipment_id")
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_own_capital_expense_certifications (
                    shipment_id, expenses_complete, actor, certified_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(shipment_id) DO UPDATE SET
                    expenses_complete = excluded.expenses_complete,
                    actor = excluded.actor,
                    certified_at = excluded.certified_at
                """,
                (shipment_id, 1 if expenses_complete else 0, str(actor or ""), now),
            )
            conn.commit()
        result = self.recalculate()
        return {
            "shipment_id": shipment_id,
            "expenses_complete": bool(expenses_complete),
            "certified_at": now,
            "daily_rows_changed": result.daily_rows_changed,
        }

    def record_ff_receipt(
        self,
        *,
        movement_id: str,
        shipment_id: str,
        effective_date: str,
        quantities_by_nm: Mapping[int, Any],
        expenses_complete: bool,
    ) -> dict[str, Any]:
        return self._record_stage_transfer(
            movement_id=movement_id,
            event_type=EVENT_STAGE_TRANSFER,
            shipment_id=shipment_id,
            supply_id="",
            effective_date=effective_date,
            stage_from=STAGE_PRODUCTION_TO_FF,
            stage_to=STAGE_FF,
            quantities_by_nm=quantities_by_nm,
            expenses_complete=expenses_complete,
        )

    def record_supplier_dispatch(
        self,
        *,
        movement_id: str,
        shipment_id: str,
        effective_date: str,
        quantities_by_nm: Mapping[int, Any],
    ) -> dict[str, Any]:
        return self._record_stage_transfer(
            movement_id=movement_id,
            event_type=EVENT_STAGE_TRANSFER,
            shipment_id=shipment_id,
            supply_id="",
            effective_date=effective_date,
            stage_from=STAGE_PRODUCTION,
            stage_to=STAGE_PRODUCTION_TO_FF,
            quantities_by_nm=quantities_by_nm,
            expenses_complete=True,
        )

    def materialize_supplier_boundaries(
        self,
        *,
        shipment_id: str,
        actual_shipment_date: str | None,
        actual_ff_acceptance_date: str | None,
        expenses_complete: bool,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        shipment_id = _required_text(shipment_id, "shipment_id")
        result: dict[str, Any] = {"shipment_id": shipment_id}
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            payment_events = [dict(row) for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_own_capital_events
                WHERE shipment_id = ? AND event_type = ?
                ORDER BY effective_date, event_id
                """,
                (shipment_id, EVENT_SUPPLIER_PAYMENT),
            ).fetchall()]
            cost_events = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_own_capital_events
                    WHERE shipment_id = ? AND event_type = ?
                    ORDER BY effective_date, event_id
                    """,
                    (shipment_id, EVENT_COST_PAYMENT),
                ).fetchall()
            ]
        for event in payment_events:
            event_date = str(event["effective_date"])
            desired_stage = _physical_stage_for_supplier_payment(
                effective_date=event_date,
                actual_shipment_date=actual_shipment_date,
                actual_ff_acceptance_date=actual_ff_acceptance_date,
            )
            original_stage = str(event["stage_to"])
            if desired_stage == original_stage:
                continue
            payload = _json_loads(event.get("payload_json"))
            payment_id = str(payload.get("payment_id") or event["event_id"])
            now = self.timestamp_factory()
            with _connect(self.runtime.db_path) as conn:
                _ensure_own_capital_schema(conn)
                self._insert_event(
                    conn,
                    event_id=f"supplier_payment_stage_correction:{event['event_id']}",
                    event_type=EVENT_STAGE_TRANSFER,
                    effective_date=event_date,
                    shipment_id=shipment_id,
                    supply_id="",
                    nm_id=int(event["nm_id"]),
                    stage_from=original_stage,
                    stage_to=desired_stage,
                    quantity=_decimal(event["quantity"]),
                    capital_rub=_decimal(event["capital_rub"]),
                    confirmed_quantity=_decimal(event["confirmed_quantity"]),
                    cost_layer_id=str(event["cost_layer_id"]),
                    warehouse="",
                    destination="",
                    payload={
                        "payment_id": payment_id,
                        "boundary_correction": True,
                        "expenses_complete": True,
                        "confirmation_reason": "paid_purchase_cost",
                    },
                    created_at=now,
                )
                conn.commit()
        for event in cost_events:
            event_date = str(event["effective_date"])
            desired_stage = _physical_stage_for_supplier_payment(
                effective_date=event_date,
                actual_shipment_date=actual_shipment_date,
                actual_ff_acceptance_date=actual_ff_acceptance_date,
            )
            with _connect(self.runtime.db_path) as conn:
                _ensure_own_capital_schema(conn)
                correction_rows = conn.execute(
                    """
                    SELECT stage_to, payload_json
                    FROM sheet_vitrina_v1_own_capital_events
                    WHERE event_type = ? AND event_id LIKE ? ESCAPE '\\'
                    ORDER BY created_at, event_id
                    """,
                    (
                        EVENT_STAGE_TRANSFER,
                        _literal_like_prefix(
                            f"expense_stage_correction:{event['event_id']}:"
                        ),
                    ),
                ).fetchall()
                current_stage = (
                    str(correction_rows[-1]["stage_to"])
                    if correction_rows
                    else str(event["stage_to"])
                )
                if desired_stage == current_stage:
                    continue
                self._insert_event(
                    conn,
                    event_id=(
                        f"expense_stage_correction:{event['event_id']}:"
                        f"{desired_stage}"
                    ),
                    event_type=EVENT_STAGE_TRANSFER,
                    effective_date=event_date,
                    shipment_id=shipment_id,
                    supply_id="",
                    nm_id=int(event["nm_id"]),
                    stage_from=current_stage,
                    stage_to=desired_stage,
                    quantity=ZERO,
                    capital_rub=_decimal(event["capital_rub"]),
                    confirmed_quantity=ZERO,
                    cost_layer_id=str(event["cost_layer_id"]),
                    warehouse="",
                    destination="",
                    payload={
                        "expense_event_id": str(event["event_id"]),
                        "boundary_correction": True,
                        "expenses_complete": bool(expenses_complete),
                        "confirmation_reason": (
                            "expense_completeness_certified"
                            if expenses_complete
                            else "expense_completeness_not_certified"
                        ),
                    },
                    created_at=self.timestamp_factory(),
                )
                conn.commit()
        if actual_shipment_date:
            ship_date = _iso_date(actual_shipment_date, "actual_shipment_date")
            dispatch_qty: dict[int, Decimal] = {}
            for event in payment_events:
                if str(event["effective_date"]) <= ship_date and str(event["stage_to"]) == STAGE_PRODUCTION:
                    nm_id = int(event["nm_id"])
                    dispatch_qty[nm_id] = dispatch_qty.get(nm_id, ZERO) + _decimal(event["quantity"])
            if dispatch_qty:
                result["dispatch"] = self.record_supplier_dispatch(
                    movement_id=f"supplier_dispatch:{shipment_id}",
                    shipment_id=shipment_id,
                    effective_date=ship_date,
                    quantities_by_nm=dispatch_qty,
                )
        if actual_ff_acceptance_date:
            acceptance_date = _iso_date(actual_ff_acceptance_date, "actual_ff_acceptance_date")
            receipt_qty: dict[int, Decimal] = {}
            for event in payment_events:
                if str(event["effective_date"]) > acceptance_date:
                    continue
                if str(event["stage_to"]) not in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                    continue
                nm_id = int(event["nm_id"])
                receipt_qty[nm_id] = receipt_qty.get(nm_id, ZERO) + _decimal(event["quantity"])
            if receipt_qty:
                result["ff_receipt"] = self.record_ff_receipt(
                    movement_id=f"supplier_ff_acceptance:{shipment_id}",
                    shipment_id=shipment_id,
                    effective_date=acceptance_date,
                    quantities_by_nm=receipt_qty,
                    expenses_complete=expenses_complete,
                )
        if payment_events and recalculate:
            result["recalculate"] = self.recalculate().__dict__
        return result

    def record_ff_writeoff(
        self,
        *,
        supply_id: str,
        effective_date: str,
        sent_quantities_by_nm: Mapping[int, Any],
        warehouse: str,
        destination: str,
        known_nm_ids: Iterable[int] | None = None,
        expenses_complete: bool = False,
    ) -> dict[str, Any]:
        allowed = {int(item) for item in (known_nm_ids or [])}
        if allowed:
            unknown = sorted(int(nm_id) for nm_id in sent_quantities_by_nm if int(nm_id) not in allowed)
            if unknown:
                self._record_blocker(
                    code="unknown_wb_nmid",
                    source_identity=str(supply_id),
                    details={"nm_ids": unknown},
                )
                raise ValueError(f"WB supply contains nmID absent from authoritative nomenclature: {unknown}")
        return self._record_stage_transfer(
            movement_id=f"wb_supply:{_required_text(supply_id, 'supply_id')}",
            event_type=EVENT_STAGE_TRANSFER,
            shipment_id="",
            supply_id=supply_id,
            effective_date=effective_date,
            stage_from=STAGE_FF,
            stage_to=STAGE_FF_TO_WB,
            quantities_by_nm=sent_quantities_by_nm,
            expenses_complete=expenses_complete,
            warehouse=warehouse,
            destination=destination,
        )

    def record_ordinary_wb_supply_final(
        self,
        *,
        supply_id: str,
        writeoff_date: str,
        acceptance_date: str,
        sent_quantities_by_nm: Mapping[int, Any],
        accepted_quantities_by_nm: Mapping[int, Any],
        warehouse: str,
        destination: str,
        known_nm_ids: Iterable[int] | None = None,
        expenses_complete: bool = False,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        return self.record_ordinary_wb_supply_acceptance(
            supply_id=supply_id,
            writeoff_date=writeoff_date,
            acceptance_date=acceptance_date,
            sent_quantities_by_nm=sent_quantities_by_nm,
            accepted_quantities_by_nm=accepted_quantities_by_nm,
            warehouse=warehouse,
            destination=destination,
            known_nm_ids=known_nm_ids,
            expenses_complete=expenses_complete,
            final=True,
            recalculate=recalculate,
        )

    def record_ordinary_wb_supply_acceptance(
        self,
        *,
        supply_id: str,
        writeoff_date: str,
        acceptance_date: str,
        sent_quantities_by_nm: Mapping[int, Any],
        accepted_quantities_by_nm: Mapping[int, Any],
        physical_sent_quantities_by_nm: Mapping[int, Any] | None = None,
        physical_accepted_quantities_by_nm: Mapping[int, Any] | None = None,
        warehouse: str,
        destination: str,
        known_nm_ids: Iterable[int] | None = None,
        expenses_complete: bool = False,
        final: bool = False,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        supply_id = _required_text(supply_id, "supply_id")
        writeoff_date = _iso_date(writeoff_date, "writeoff_date")
        acceptance_date = _iso_date(acceptance_date, "acceptance_date")
        if acceptance_date < writeoff_date:
            self._record_blocker(
                code="wb_acceptance_before_writeoff",
                source_identity=supply_id,
                details={
                    "writeoff_date": writeoff_date,
                    "acceptance_date": acceptance_date,
                },
            )
            raise ValueError("WB acceptance date is earlier than FF writeoff date")
        normalized_sent = {
            _positive_int(nm_id, "nm_id"): _positive_decimal(
                quantity, f"sent[{nm_id}]"
            )
            for nm_id, quantity in sent_quantities_by_nm.items()
        }
        normalized_accepted = {
            _positive_int(nm_id, "nm_id"): _nonnegative_decimal(
                quantity, f"accepted[{nm_id}]"
            )
            for nm_id, quantity in accepted_quantities_by_nm.items()
        }
        physical_sent = {
            _positive_int(nm_id, "nm_id"): _positive_decimal(
                quantity, f"physical_sent[{nm_id}]"
            )
            for nm_id, quantity in (
                physical_sent_quantities_by_nm or normalized_sent
            ).items()
        }
        physical_accepted = {
            _positive_int(nm_id, "nm_id"): _nonnegative_decimal(
                quantity, f"physical_accepted[{nm_id}]"
            )
            for nm_id, quantity in (
                physical_accepted_quantities_by_nm or normalized_accepted
            ).items()
        }
        if final:
            missing = sorted(set(normalized_sent) - set(normalized_accepted))
            if missing:
                raise ValueError(f"final accepted quantity is missing for nmID {missing}")
        for nm_id, accepted in normalized_accepted.items():
            if nm_id not in normalized_sent:
                raise ValueError(f"accepted quantity has unknown sent nmID {nm_id}")
            if accepted > normalized_sent[nm_id]:
                self._record_blocker(
                    code="accepted_quantity_exceeds_sent",
                    source_identity=supply_id,
                    details={
                        "nm_id": nm_id,
                        "sent": _text_decimal(normalized_sent[nm_id]),
                        "accepted": _text_decimal(accepted),
                    },
                )
                raise ValueError("accepted quantity exceeds sent quantity")
        writeoff = self.record_ff_writeoff(
            supply_id=supply_id,
            effective_date=writeoff_date,
            sent_quantities_by_nm=normalized_sent,
            warehouse=warehouse,
            destination=destination,
            known_nm_ids=known_nm_ids,
            expenses_complete=expenses_complete,
        )
        supply_layers = self._wb_supply_layers(supply_id)
        planned: list[dict[str, Any]] = []
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            for nm_id, sent in normalized_sent.items():
                accepted = normalized_accepted.get(nm_id, ZERO)
                previous = sum(
                    (
                        _decimal(row["quantity"])
                        for row in conn.execute(
                            """
                            SELECT quantity
                            FROM sheet_vitrina_v1_own_capital_events
                            WHERE supply_id = ? AND nm_id = ? AND event_type = ?
                            ORDER BY effective_date, event_id
                            """,
                            (supply_id, nm_id, EVENT_WB_ACCEPTANCE),
                        ).fetchall()
                    ),
                    ZERO,
                )
                if accepted < previous:
                    self._record_blocker(
                        code="accepted_quantity_regressed",
                        source_identity=supply_id,
                        details={
                            "nm_id": nm_id,
                            "previous_accepted": _text_decimal(previous),
                            "current_accepted": _text_decimal(accepted),
                        },
                    )
                    raise ValueError("accepted quantity regressed for WB supply")
                delta = accepted - previous
                supply_layer = supply_layers.get(nm_id)
                unit_cost = (
                    _safe_div(supply_layer["capital"], supply_layer["quantity"])
                    if supply_layer is not None
                    else None
                )
                if unit_cost is None and delta > ZERO:
                    raise ValueError(
                        f"original FF → WB supply cost snapshot is missing for nmID {nm_id}"
                    )
                confirmed_share = (
                    _safe_div(
                        supply_layer["confirmed_quantity"], supply_layer["quantity"]
                    )
                    if supply_layer is not None
                    else ZERO
                ) or ZERO
                planned.append(
                    {
                        "nm_id": nm_id,
                        "sent": sent,
                        "accepted": accepted,
                        "accepted_before": previous,
                        "accepted_delta": delta,
                        "outstanding": sent - accepted,
                        "physical_sent": physical_sent.get(nm_id, sent),
                        "physical_accepted": physical_accepted.get(nm_id, accepted),
                        "physical_outstanding": max(
                            ZERO,
                            physical_sent.get(nm_id, sent)
                            - physical_accepted.get(nm_id, accepted),
                        ),
                        "unit_cost": unit_cost or ZERO,
                        "confirmed_share": confirmed_share,
                    }
                )
            for item in planned:
                if item["accepted_delta"] > ZERO:
                    cumulative_key = _text_decimal(item["accepted"]).replace(".", "_")
                    self._insert_event(
                        conn,
                        event_id=(
                            f"wb_acceptance:{supply_id}:{item['nm_id']}:"
                            f"cumulative_{cumulative_key}"
                        ),
                        event_type=EVENT_WB_ACCEPTANCE,
                        effective_date=acceptance_date,
                        shipment_id="",
                        supply_id=supply_id,
                        nm_id=item["nm_id"],
                        stage_from=STAGE_FF_TO_WB,
                        stage_to=STAGE_WB,
                        quantity=item["accepted_delta"],
                        capital_rub=item["accepted_delta"] * item["unit_cost"],
                        confirmed_quantity=(
                            item["accepted_delta"] * item["confirmed_share"]
                        ),
                        cost_layer_id=f"wb_supply:{supply_id}:{item['nm_id']}",
                        warehouse=warehouse,
                        destination=destination,
                        payload={
                            "accepted_quantity_source": (
                                "final_fact" if final else "partial_fact"
                            ),
                            "sent_quantity": _text_decimal(item["sent"]),
                            "accepted_quantity_before": _text_decimal(
                                item["accepted_before"]
                            ),
                            "accepted_quantity_cumulative": _text_decimal(
                                item["accepted"]
                            ),
                            "accepted_quantity_delta": _text_decimal(
                                item["accepted_delta"]
                            ),
                            "outstanding_quantity": _text_decimal(
                                item["outstanding"]
                            ),
                            "final": bool(final),
                        },
                        created_at=now,
                    )
                if final:
                    existing_outstanding = conn.execute(
                        """
                        SELECT total_quantity, open_quantity,
                               physical_total_quantity, physical_open_quantity
                        FROM sheet_vitrina_v1_own_capital_wb_outstanding
                        WHERE original_supply_id = ? AND nm_id = ?
                        """,
                        (supply_id, item["nm_id"]),
                    ).fetchone()
                    reconciled = (
                        max(
                            ZERO,
                            _decimal(existing_outstanding["total_quantity"])
                            - _decimal(existing_outstanding["open_quantity"]),
                        )
                        if existing_outstanding is not None
                        else ZERO
                    )
                    open_quantity = max(ZERO, item["outstanding"] - reconciled)
                    physical_reconciled = (
                        max(
                            ZERO,
                            _decimal(existing_outstanding["physical_total_quantity"])
                            - _decimal(existing_outstanding["physical_open_quantity"]),
                        )
                        if existing_outstanding is not None
                        else ZERO
                    )
                    physical_open_quantity = max(
                        ZERO,
                        item["physical_outstanding"] - physical_reconciled,
                    )
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_own_capital_wb_outstanding (
                            original_supply_id, nm_id, warehouse, destination, original_cost_layer_id,
                            total_quantity, open_quantity,
                            physical_total_quantity, physical_open_quantity,
                            unit_cost_rub, writeoff_date,
                            confirmed_share, final_acceptance_date, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(original_supply_id, nm_id) DO UPDATE SET
                            warehouse = excluded.warehouse,
                            destination = excluded.destination,
                            total_quantity = excluded.total_quantity,
                            open_quantity = excluded.open_quantity,
                            physical_total_quantity = excluded.physical_total_quantity,
                            physical_open_quantity = excluded.physical_open_quantity,
                            unit_cost_rub = excluded.unit_cost_rub,
                            confirmed_share = excluded.confirmed_share,
                            updated_at = excluded.updated_at
                        """,
                        (
                            supply_id,
                            item["nm_id"],
                            str(warehouse or ""),
                            str(destination or ""),
                            f"wb_supply:{supply_id}:{item['nm_id']}",
                            _text_decimal(item["outstanding"]),
                            _text_decimal(open_quantity),
                            _text_decimal(item["physical_outstanding"]),
                            _text_decimal(physical_open_quantity),
                            _text_decimal(item["unit_cost"]),
                            _iso_date(writeoff_date, "writeoff_date"),
                            _text_decimal(item["confirmed_share"]),
                            acceptance_date,
                            now,
                            now,
                        ),
                    )
            conn.commit()
        if recalculate:
            self.recalculate()
        return {
            "status": "ok",
            "writeoff": writeoff,
            "supply_id": supply_id,
            "final": bool(final),
            "lines": [
                {
                    key: (_text_decimal(value) if isinstance(value, Decimal) else value)
                    for key, value in item.items()
                }
                for item in planned
            ],
        }

    def reconcile_doprinato(
        self,
        *,
        reconciliation_supply_id: str,
        effective_date: str,
        quantities_by_nm: Mapping[int, Any],
        warehouse: str,
        destination: str,
        original_supply_id: str | None = None,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        reconciliation_supply_id = _required_text(reconciliation_supply_id, "reconciliation_supply_id")
        effective_date = _iso_date(effective_date, "effective_date")
        requested = {
            _positive_int(nm_id, "nm_id"): _positive_decimal(qty, f"quantity[{nm_id}]")
            for nm_id, qty in quantities_by_nm.items()
        }
        if not requested:
            raise ValueError("Допринято requires positive SKU quantities")
        _validate_targeted_orphan_doprinato_document_guard(
            reconciliation_supply_id=reconciliation_supply_id,
            effective_date=effective_date,
            requested=requested,
            warehouse=warehouse,
            original_supply_id=original_supply_id,
        )
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            existing = conn.execute(
                """
                SELECT event_id, nm_id, quantity, capital_rub, confirmed_quantity,
                       payload_json
                FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id LIKE ? ESCAPE '\\'
                ORDER BY event_id
                """,
                (_literal_like_prefix(f"wb_reconciliation:{reconciliation_supply_id}:"),),
            ).fetchall()
            if existing:
                _validate_targeted_orphan_doprinato_idempotency_guard(
                    reconciliation_supply_id=reconciliation_supply_id,
                    existing_events=existing,
                )
                return {"status": "ok", "idempotent": True, "reconciliation_supply_id": reconciliation_supply_id}
            closures: list[dict[str, Any]] = []
            classifications: list[dict[str, Any]] = []
            for nm_id, quantity in requested.items():
                params: list[Any] = [nm_id]
                where = ["nm_id = ?", "final_acceptance_date <= ?"]
                params.append(effective_date)
                if original_supply_id:
                    where.append("original_supply_id = ?")
                    params.append(str(original_supply_id))
                else:
                    where.extend(["warehouse = ?", "destination = ?"])
                    params.extend([str(warehouse or ""), str(destination or "")])
                candidates = conn.execute(
                    f"""
                    SELECT * FROM sheet_vitrina_v1_own_capital_wb_outstanding
                    WHERE {' AND '.join(where)}
                    ORDER BY final_acceptance_date ASC, original_supply_id ASC
                    """,
                    tuple(params),
                ).fetchall()
                targeted_orphan = _plan_targeted_orphan_doprinato_classification(
                    reconciliation_supply_id=reconciliation_supply_id,
                    effective_date=effective_date,
                    requested=requested,
                    nm_id=nm_id,
                    quantity=quantity,
                    warehouse=warehouse,
                    original_supply_id=original_supply_id,
                    candidates=candidates,
                )
                if targeted_orphan is not None:
                    classifications.append(targeted_orphan)
                    continue
                remaining = quantity
                for candidate in candidates:
                    if remaining <= ZERO:
                        break
                    open_qty = _decimal(candidate["open_quantity"])
                    physical_open = _decimal(candidate["physical_open_quantity"])
                    if physical_open <= ZERO:
                        continue
                    physical_closed = min(physical_open, remaining)
                    closed = min(open_qty, physical_closed)
                    closures.append(
                        {
                            "nm_id": nm_id,
                            "original_supply_id": str(candidate["original_supply_id"]),
                            "closed": closed,
                            "unit_cost": _decimal(candidate["unit_cost_rub"]),
                            "confirmed_share": _decimal(candidate["confirmed_share"]),
                            "cost_layer_id": str(candidate["original_cost_layer_id"]),
                            "open_before": open_qty,
                            "physical_open_before": physical_open,
                            "physical_closed": physical_closed,
                        }
                    )
                    remaining -= physical_closed
                if remaining > ZERO:
                    self._record_blocker(
                        code="doprinato_unmatched_surplus",
                        source_identity=reconciliation_supply_id,
                        details={
                            "nm_id": nm_id,
                            "requested": _text_decimal(quantity),
                            "unmatched_physical_surplus": _text_decimal(remaining),
                        },
                    )
                    raise ValueError("Допринято quantity exceeds matching outstanding quantity")
            for index, closure in enumerate(closures, start=1):
                self._insert_event(
                    conn,
                    event_id=(
                        f"wb_reconciliation:{reconciliation_supply_id}:"
                        f"{closure['original_supply_id']}:{closure['nm_id']}:{index}"
                    ),
                    event_type=EVENT_WB_RECONCILIATION,
                    effective_date=effective_date,
                    shipment_id="",
                    supply_id=reconciliation_supply_id,
                    nm_id=closure["nm_id"],
                    stage_from=STAGE_FF_TO_WB,
                    stage_to=STAGE_WB,
                    quantity=closure["closed"],
                    capital_rub=closure["closed"] * closure["unit_cost"],
                    confirmed_quantity=closure["closed"] * closure["confirmed_share"],
                    cost_layer_id=closure["cost_layer_id"],
                    warehouse=warehouse,
                    destination=destination,
                    payload={
                        "original_supply_id": closure["original_supply_id"],
                        "matching": "direct" if original_supply_id else "warehouse_destination_sku_fifo",
                        "no_ff_writeoff": True,
                        "physical_quantity": _text_decimal(
                            closure["physical_closed"]
                        ),
                        "untracked_physical_quantity": _text_decimal(
                            closure["physical_closed"] - closure["closed"]
                        ),
                    },
                    created_at=now,
                )
                next_open = closure["open_before"] - closure["closed"]
                next_physical_open = (
                    closure["physical_open_before"] - closure["physical_closed"]
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_own_capital_wb_outstanding
                    SET open_quantity = ?, physical_open_quantity = ?, updated_at = ?
                    WHERE original_supply_id = ? AND nm_id = ?
                    """,
                    (
                        _text_decimal(next_open),
                        _text_decimal(next_physical_open),
                        now,
                        closure["original_supply_id"],
                        closure["nm_id"],
                    ),
                )
            for classification in classifications:
                self._insert_event(
                    conn,
                    event_id=classification["event_id"],
                    event_type=EVENT_WB_RECONCILIATION,
                    effective_date=effective_date,
                    shipment_id="",
                    supply_id=reconciliation_supply_id,
                    nm_id=classification["nm_id"],
                    stage_from=STAGE_FF_TO_WB,
                    stage_to=STAGE_WB,
                    quantity=ZERO,
                    capital_rub=ZERO,
                    confirmed_quantity=ZERO,
                    cost_layer_id=classification["reason"],
                    warehouse=warehouse,
                    destination=destination,
                    payload={
                        "classification": classification["reason"],
                        "original_supply_id": "",
                        "fifo_candidate_supply_id": classification[
                            "fifo_candidate_supply_id"
                        ],
                        "requested_quantity": _text_decimal(
                            classification["requested_quantity"]
                        ),
                        "tracked_outstanding": "0",
                        "physical_outstanding": "0",
                        "paid_capital_outstanding": "0",
                        "no_ff_writeoff": True,
                        "zero_capital": True,
                        "targeted": True,
                    },
                    created_at=now,
                )
            conn.commit()
        if recalculate:
            self.recalculate()
        return {
            "status": "ok",
            "idempotent": False,
            "reconciliation_supply_id": reconciliation_supply_id,
            "closures": [
                {
                    "original_supply_id": item["original_supply_id"],
                    "nm_id": item["nm_id"],
                    "quantity": _text_decimal(item["closed"]),
                    "physical_quantity": _text_decimal(item["physical_closed"]),
                    "untracked_physical_quantity": _text_decimal(
                        item["physical_closed"] - item["closed"]
                    ),
                }
                for item in closures
            ],
            "classifications": [
                {
                    "event_id": item["event_id"],
                    "reason": item["reason"],
                    "nm_id": item["nm_id"],
                    "quantity": _text_decimal(item["requested_quantity"]),
                    "fifo_candidate_supply_id": item[
                        "fifo_candidate_supply_id"
                    ],
                    "capital_rub": "0",
                }
                for item in classifications
            ],
        }

    def recalculate(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> OwnProductCapitalRebuildResult:
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            _ensure_own_capital_schema(conn)
            events = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_own_capital_events ORDER BY effective_date, created_at, event_id"
            ).fetchall()]
            certifications = {
                str(row["shipment_id"]): bool(row["expenses_complete"])
                for row in conn.execute(
                    "SELECT shipment_id, expenses_complete FROM sheet_vitrina_v1_own_capital_expense_certifications"
                ).fetchall()
            }
            blockers = int(conn.execute(
                "SELECT COUNT(*) AS count FROM sheet_vitrina_v1_own_capital_blockers WHERE resolved_at IS NULL"
            ).fetchone()["count"])
            if not events:
                return OwnProductCapitalRebuildResult(0, 0, 0, blockers, _stable_hash([]))
            first_date = min(str(item["effective_date"]) for item in events)
            last_event_date = max(str(item["effective_date"]) for item in events)
            start = _iso_date(date_from or first_date, "date_from")
            end = _iso_date(date_to or max(last_event_date, date.today().isoformat()), "date_to")
            if end < start:
                raise ValueError("date_to must be on or after date_from")
            wb_rows = [dict(row) for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_cost_daily_state
                WHERE as_of_date BETWEEN ? AND ?
                ORDER BY as_of_date, nm_id
                """,
                (start, end),
            ).fetchall()]
            wb_by_date: dict[str, dict[int, dict[str, Any]]] = {}
            for row in wb_rows:
                wb_by_date.setdefault(str(row["as_of_date"]), {})[int(row["nm_id"])] = row
            outstanding_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_own_capital_wb_outstanding"
            ).fetchall()]
            events_by_date: dict[str, list[dict[str, Any]]] = {}
            reconciliations_by_outstanding: dict[tuple[str, int], list[tuple[str, Decimal]]] = {}
            for event in events:
                events_by_date.setdefault(str(event["effective_date"]), []).append(event)
                if str(event.get("event_type") or "") == EVENT_WB_RECONCILIATION:
                    original_supply_id = str(
                        _json_loads(event.get("payload_json")).get("original_supply_id") or ""
                    )
                    if original_supply_id:
                        reconciliations_by_outstanding.setdefault(
                            (original_supply_id, int(event["nm_id"])), []
                        ).append((str(event["effective_date"]), _decimal(event["quantity"])))
            state: dict[int, dict[str, dict[str, Any]]] = {}
            for event in events:
                if str(event["effective_date"]) >= start:
                    break
                _apply_event(state, event, certifications=certifications)
            changed = 0
            dates = list(_date_range(start, end))
            for current_date in dates:
                for event in events_by_date.get(current_date, []):
                    _apply_event(state, event, certifications=certifications)
                for nm_id, wb in wb_by_date.get(current_date, {}).items():
                    stock_qty = _decimal(wb.get("stock_qty"))
                    unit_cost = _optional_decimal(wb.get("our_wb_unit_cost_rub"))
                    confirmed_qty = min(stock_qty, _decimal(wb.get("confirmed_qty")))
                    state.setdefault(nm_id, {})[STAGE_WB] = {
                        "qty": stock_qty,
                        "capital": stock_qty * unit_cost if unit_cost is not None else ZERO,
                        "confirmed_qty": confirmed_qty,
                        "reasons": _component_reasons(wb.get("component_status_json")),
                    }
                open_by_nm: dict[int, Decimal] = {}
                for row in outstanding_rows:
                    if str(row.get("final_acceptance_date") or "") <= current_date:
                        historical_open = _decimal(row["total_quantity"])
                        for reconciliation_date, reconciled_quantity in reconciliations_by_outstanding.get(
                            (str(row["original_supply_id"]), int(row["nm_id"])), []
                        ):
                            if reconciliation_date <= current_date:
                                historical_open -= reconciled_quantity
                        historical_open = max(ZERO, historical_open)
                        open_by_nm[int(row["nm_id"])] = (
                            open_by_nm.get(int(row["nm_id"]), ZERO) + historical_open
                        )
                for nm_id, stages in state.items():
                    for stage in OWN_PRODUCT_CAPITAL_STAGES:
                        bucket = stages.get(stage, _empty_bucket())
                        if stage == STAGE_FF_TO_WB and open_by_nm.get(nm_id, ZERO) > ZERO:
                            bucket.setdefault("reasons", []).append(
                                f"Недопринято WB: {_text_decimal(open_by_nm[nm_id])} шт"
                            )
                        fingerprint = _stable_hash(
                            {
                                "as_of_date": current_date,
                                "nm_id": nm_id,
                                "stage": stage,
                                "qty": _text_decimal(bucket["qty"]),
                                "capital": _text_decimal(bucket["capital"]),
                                "confirmed_qty": _text_decimal(bucket["confirmed_qty"]),
                                "reasons": sorted(set(bucket.get("reasons") or [])),
                            }
                        )
                        existing = conn.execute(
                            """
                            SELECT input_fingerprint FROM sheet_vitrina_v1_own_capital_daily_state
                            WHERE as_of_date = ? AND nm_id = ? AND stage = ?
                            """,
                            (current_date, nm_id, stage),
                        ).fetchone()
                        if existing is not None and str(existing["input_fingerprint"]) == fingerprint:
                            continue
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_own_capital_daily_state (
                                as_of_date, nm_id, stage, quantity, capital_rub, confirmed_quantity,
                                diagnostics_json, calculated_at, input_fingerprint
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(as_of_date, nm_id, stage) DO UPDATE SET
                                quantity = excluded.quantity,
                                capital_rub = excluded.capital_rub,
                                confirmed_quantity = excluded.confirmed_quantity,
                                diagnostics_json = excluded.diagnostics_json,
                                calculated_at = excluded.calculated_at,
                                input_fingerprint = excluded.input_fingerprint
                            """,
                            (
                                current_date,
                                nm_id,
                                stage,
                                _text_decimal(bucket["qty"]),
                                _text_decimal(bucket["capital"]),
                                _text_decimal(bucket["confirmed_qty"]),
                                _json_dumps({"reasons": sorted(set(bucket.get("reasons") or []))}),
                                self.timestamp_factory(),
                                fingerprint,
                            ),
                        )
                        changed += 1
            conn.commit()
        run_fingerprint = _stable_hash(
            {
                "event_hashes": [str(item["evidence_hash"]) for item in events],
                "date_from": start,
                "date_to": end,
            }
        )
        return OwnProductCapitalRebuildResult(len(events), len(dates), changed, blockers, run_fingerprint)

    def load_daily_metric_lookup(self, as_of_date: str) -> dict[int, dict[str, Any]]:
        as_of_date = _iso_date(as_of_date, "as_of_date")
        if as_of_date >= "2026-07-01":
            canonical = self._load_canonical_daily_metric_lookup(as_of_date)
            if canonical:
                return canonical
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_own_capital_daily_state
                WHERE as_of_date = ? ORDER BY nm_id, stage
                """,
                (as_of_date,),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            nm_id = int(row["nm_id"])
            stage = str(row["stage"])
            qty = _decimal(row["quantity"])
            capital = _decimal(row["capital_rub"])
            confirmed = _decimal(row["confirmed_quantity"])
            target = result.setdefault(nm_id, {"presentation_reasons": [], "stage_presentation": {}})
            target[own_stage_metric_key(stage, "qty")] = float(qty)
            target[own_stage_metric_key(stage, "paid_equivalent_qty")] = float(qty)
            target[own_stage_metric_key(stage, "capital_rub")] = float(capital)
            target[own_stage_metric_key(stage, "unit_cost_rub")] = (
                float(capital / qty) if qty > ZERO else None
            )
            target[own_stage_metric_key(stage, "confirmed_share_pct")] = (
                float(confirmed / qty) if qty > ZERO else None
            )
            target[own_stage_metric_key(stage, "cost_coverage_pct")] = (
                1.0 if qty > ZERO and capital > ZERO else (0.0 if qty > ZERO else None)
            )
            target[own_stage_metric_key(stage, "confirmed_qty")] = float(confirmed)
            target[own_stage_metric_key(stage, "cost_covered_qty")] = (
                float(qty) if capital > ZERO else 0.0
            )
            diagnostics = _json_loads(row.get("diagnostics_json"))
            stage_reasons = [str(item) for item in diagnostics.get("reasons") or []]
            target["presentation_reasons"].extend(stage_reasons)
            stage_unconfirmed = qty > ZERO and confirmed < qty
            target["stage_presentation"][stage] = {
                "state": "unconfirmed" if stage_unconfirmed else "confirmed",
                "reason": "; ".join(sorted(set(stage_reasons))) if stage_unconfirmed else "",
            }
        for target in result.values():
            qty = sum(_decimal(target.get(own_stage_metric_key(stage, "qty"))) for stage in OWN_PRODUCT_CAPITAL_STAGES)
            capital = sum(
                (_decimal(target.get(own_stage_metric_key(stage, "capital_rub"))) for stage in OWN_PRODUCT_CAPITAL_STAGES),
                ZERO,
            )
            confirmed = sum(
                (_decimal(target.get(own_stage_metric_key(stage, "confirmed_qty"))) for stage in OWN_PRODUCT_CAPITAL_STAGES),
                ZERO,
            )
            target[OWN_TOTAL_QTY_METRIC_KEY] = float(qty)
            target[OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY] = float(qty)
            target[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] = float(capital)
            target[OWN_AVG_COST_RUB_METRIC_KEY] = float(capital / qty) if qty > ZERO else None
            target[OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY] = float(confirmed / qty) if qty > ZERO else None
            target.setdefault(OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY, 0.0)
            target.setdefault(OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY, None)
            if qty > ZERO and confirmed < qty:
                target["presentation_state"] = "unconfirmed"
                if not target["presentation_reasons"]:
                    target["presentation_reasons"] = ["полнота расходов не подтверждена"]
            else:
                target["presentation_state"] = "confirmed"
            target["presentation_reason"] = "; ".join(sorted(set(target["presentation_reasons"])))
        return result

    def _load_canonical_daily_metric_lookup(self, as_of_date: str) -> dict[int, dict[str, Any]]:
        """Read module-45 public metrics from the unified projection after cutover."""
        from packages.application.canonical_cost_engine import (
            STAGE_FF_TO_WB,
        )

        with _connect(self.runtime.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_canonical_cost_daily_state'"
            ).fetchone() is None:
                return {}
            rows = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_canonical_cost_daily_state
                WHERE as_of_date=? ORDER BY nm_id,stage
                """,
                (as_of_date,),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            nm_id = int(row["nm_id"])
            stage = str(row["stage"])
            qty = _decimal(row["physical_quantity"])
            paid_equivalent = _decimal(row["paid_equivalent_quantity"])
            capital = _decimal(row["paid_capital_rub"])
            covered = _decimal(row["cost_covered_quantity"])
            confirmed = _decimal(row["confirmed_quantity"])
            target = result.setdefault(
                nm_id,
                {"presentation_reasons": [], "stage_presentation": {}},
            )
            target[own_stage_metric_key(stage, "qty")] = float(qty)
            target[own_stage_metric_key(stage, "paid_equivalent_qty")] = float(paid_equivalent)
            target[own_stage_metric_key(stage, "capital_rub")] = float(capital)
            target[own_stage_metric_key(stage, "unit_cost_rub")] = (
                float(capital / paid_equivalent) if paid_equivalent > ZERO else None
            )
            target[own_stage_metric_key(stage, "cost_coverage_pct")] = (
                float(covered / qty) if qty > ZERO else None
            )
            target[own_stage_metric_key(stage, "confirmed_share_pct")] = (
                float(confirmed / qty) if qty > ZERO else None
            )
            target[own_stage_metric_key(stage, "confirmed_qty")] = float(confirmed)
            target[own_stage_metric_key(stage, "cost_covered_qty")] = float(covered)
            source_quality = str(row.get("source_quality") or "")
            reasons = [] if source_quality == "primary_documents" else [source_quality or "coverage_gap"]
            target["presentation_reasons"].extend(reasons)
            target["stage_presentation"][stage] = {
                "state": "confirmed" if qty <= ZERO or confirmed >= qty else "unconfirmed",
                "reason": "; ".join(reasons),
            }
            if stage == STAGE_FF_TO_WB:
                under_qty = _decimal(row["underaccepted_quantity"])
                under_capital = _decimal(row["underaccepted_paid_capital_rub"])
                target[OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY] = float(under_qty)
                target[OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY] = (
                    float(under_capital / under_qty) if under_qty > ZERO else None
                )
        for target in result.values():
            qty = sum((_decimal(target.get(own_stage_metric_key(stage, "qty"))) for stage in OWN_PRODUCT_CAPITAL_STAGES), ZERO)
            paid_equivalent = sum((
                _decimal(target.get(own_stage_metric_key(stage, "paid_equivalent_qty")))
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            ), ZERO)
            capital = sum((
                _decimal(target.get(own_stage_metric_key(stage, "capital_rub")))
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            ), ZERO)
            confirmed = sum((
                _decimal(target.get(own_stage_metric_key(stage, "confirmed_qty")))
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            ), ZERO)
            target[OWN_TOTAL_QTY_METRIC_KEY] = float(qty)
            target[OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY] = float(paid_equivalent)
            target[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] = float(capital)
            target[OWN_AVG_COST_RUB_METRIC_KEY] = (
                float(capital / paid_equivalent) if paid_equivalent > ZERO else None
            )
            target[OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY] = (
                float(confirmed / qty) if qty > ZERO else None
            )
            target["presentation_state"] = (
                "confirmed" if qty <= ZERO or confirmed >= qty else "unconfirmed"
            )
            target["presentation_reason"] = "; ".join(
                sorted(set(target["presentation_reasons"]))
            )
        return result

    def status(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            event_count = int(conn.execute(
                "SELECT COUNT(*) AS count FROM sheet_vitrina_v1_own_capital_events"
            ).fetchone()["count"])
            latest_date = conn.execute(
                "SELECT MAX(as_of_date) AS as_of_date FROM sheet_vitrina_v1_own_capital_daily_state"
            ).fetchone()["as_of_date"]
            latest_rows = (
                conn.execute(
                    """
                    SELECT quantity, capital_rub, confirmed_quantity
                    FROM sheet_vitrina_v1_own_capital_daily_state
                    WHERE as_of_date = ?
                    """,
                    (latest_date,),
                ).fetchall()
                if latest_date
                else []
            )
            open_quantities = [
                _decimal(row["open_quantity"])
                for row in conn.execute(
                    "SELECT open_quantity FROM sheet_vitrina_v1_own_capital_wb_outstanding"
                ).fetchall()
                if _decimal(row["open_quantity"]) > ZERO
            ]
            blockers = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_own_capital_blockers WHERE resolved_at IS NULL ORDER BY created_at"
            ).fetchall()]
        return {
            "contract_name": "sheet_vitrina_v1_own_product_capital",
            "source": "WebCore",
            "event_count": event_count,
            "latest": (
                {
                    "as_of_date": str(latest_date),
                    "qty": float(sum((_decimal(row["quantity"]) for row in latest_rows), ZERO)),
                    "capital": float(sum((_decimal(row["capital_rub"]) for row in latest_rows), ZERO)),
                    "confirmed_qty": float(
                        sum((_decimal(row["confirmed_quantity"]) for row in latest_rows), ZERO)
                    ),
                }
                if latest_date
                else None
            ),
            "underaccepted_wb": {
                "rows": len(open_quantities),
                "quantity": float(sum(open_quantities, ZERO)),
            },
            "blockers": blockers,
        }

    def _record_stage_transfer(
        self,
        *,
        movement_id: str,
        event_type: str,
        shipment_id: str,
        supply_id: str,
        effective_date: str,
        stage_from: str,
        stage_to: str,
        quantities_by_nm: Mapping[int, Any],
        expenses_complete: bool,
        warehouse: str = "",
        destination: str = "",
    ) -> dict[str, Any]:
        movement_id = _required_text(movement_id, "movement_id")
        effective_date = _iso_date(effective_date, "effective_date")
        stage_from = _stage(stage_from)
        stage_to = _stage(stage_to)
        quantities = {
            _positive_int(nm_id, "nm_id"): _positive_decimal(qty, f"quantity[{nm_id}]")
            for nm_id, qty in quantities_by_nm.items()
        }
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            persisted = conn.execute(
                """
                SELECT event_id, effective_date, stage_from, stage_to, nm_id, quantity,
                       supply_id, warehouse, destination
                FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id LIKE ? ESCAPE '\\'
                """,
                (_literal_like_prefix(f"stage_transfer:{movement_id}:"),),
            ).fetchall()
        if persisted:
            expected_ids = {f"stage_transfer:{movement_id}:{nm_id}" for nm_id in quantities}
            persisted_ids = {str(row["event_id"]) for row in persisted}
            if persisted_ids != expected_ids:
                raise ValueError("movement identity already exists with a different SKU allocation")
            persisted_by_nm = {int(row["nm_id"]): row for row in persisted}
            for nm_id, quantity in quantities.items():
                row = persisted_by_nm[nm_id]
                if (
                    str(row["effective_date"]) != effective_date
                    or str(row["stage_from"]) != stage_from
                    or str(row["stage_to"]) != stage_to
                    or _decimal(row["quantity"]) != quantity
                    or str(row["supply_id"] or "") != str(supply_id or "")
                    or str(row["warehouse"] or "") != str(warehouse or "")
                    or str(row["destination"] or "") != str(destination or "")
                ):
                    raise ValueError("movement identity already exists with different factual evidence")
            return {
                "status": "ok",
                "idempotent": True,
                "movement_id": movement_id,
                "stage_from": stage_from,
                "stage_to": stage_to,
            }
        state = self._state_as_of(effective_date)
        planned: list[dict[str, Any]] = []
        for nm_id, quantity in quantities.items():
            bucket = state.get(nm_id, {}).get(stage_from, _empty_bucket())
            if bucket["qty"] < quantity:
                self._record_blocker(
                    code="stage_transfer_insufficient_quantity",
                    source_identity=movement_id,
                    details={
                        "nm_id": nm_id,
                        "stage": stage_from,
                        "available": _text_decimal(bucket["qty"]),
                        "requested": _text_decimal(quantity),
                    },
                )
                raise ValueError(f"insufficient {stage_from} quantity for nmID {nm_id}")
            unit_cost = _safe_div(bucket["capital"], bucket["qty"])
            if unit_cost is None:
                raise ValueError(f"moving weighted cost is missing for nmID {nm_id}")
            confirmed_share = _safe_div(bucket["confirmed_qty"], bucket["qty"]) or ZERO
            if not expenses_complete and stage_to in {STAGE_FF, STAGE_FF_TO_WB, STAGE_WB}:
                confirmed_share = ZERO
            planned.append(
                {
                    "nm_id": nm_id,
                    "quantity": quantity,
                    "capital": quantity * unit_cost,
                    "confirmed_quantity": quantity * confirmed_share,
                    "unit_cost": unit_cost,
                }
            )
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            for item in planned:
                self._insert_event(
                    conn,
                    event_id=f"stage_transfer:{movement_id}:{item['nm_id']}",
                    event_type=event_type,
                    effective_date=effective_date,
                    shipment_id=shipment_id,
                    supply_id=supply_id,
                    nm_id=item["nm_id"],
                    stage_from=stage_from,
                    stage_to=stage_to,
                    quantity=item["quantity"],
                    capital_rub=item["capital"],
                    confirmed_quantity=item["confirmed_quantity"],
                    cost_layer_id=f"movement:{movement_id}:{item['nm_id']}",
                    warehouse=warehouse,
                    destination=destination,
                    payload={
                        "moving_weighted_unit_cost_rub": _text_decimal(item["unit_cost"]),
                        "expenses_complete": bool(expenses_complete),
                        "confirmation_reason": (
                            "complete_cost_chain" if expenses_complete else "expense_completeness_not_certified"
                        ),
                    },
                    created_at=now,
                )
            conn.commit()
        return {
            "status": "ok",
            "movement_id": movement_id,
            "stage_from": stage_from,
            "stage_to": stage_to,
            "lines": [
                {
                    "nm_id": item["nm_id"],
                    "quantity": _text_decimal(item["quantity"]),
                    "capital_rub": _text_decimal(item["capital"]),
                    "unit_cost_rub": _text_decimal(item["unit_cost"]),
                }
                for item in planned
            ],
        }

    def _state_as_of(self, as_of_date: str) -> dict[int, dict[str, dict[str, Any]]]:
        as_of_date = _iso_date(as_of_date, "as_of_date")
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            events = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_own_capital_events
                WHERE effective_date <= ?
                ORDER BY effective_date, created_at, event_id
                """,
                (as_of_date,),
            ).fetchall()
            certifications = {
                str(row["shipment_id"]): bool(row["expenses_complete"])
                for row in conn.execute(
                    "SELECT shipment_id, expenses_complete FROM sheet_vitrina_v1_own_capital_expense_certifications"
                ).fetchall()
            }
        state: dict[int, dict[str, dict[str, Any]]] = {}
        for event in events:
            _apply_event(state, dict(event), certifications=certifications)
        return state

    def _wb_supply_layers(self, supply_id: str) -> dict[int, dict[str, Decimal]]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            rows = conn.execute(
                """
                SELECT nm_id, quantity, capital_rub, confirmed_quantity
                FROM sheet_vitrina_v1_own_capital_events
                WHERE supply_id = ? AND event_type = ?
                  AND stage_from = ? AND stage_to = ?
                ORDER BY event_id
                """,
                (supply_id, EVENT_STAGE_TRANSFER, STAGE_FF, STAGE_FF_TO_WB),
            ).fetchall()
        return {
            int(row["nm_id"]): {
                "quantity": _decimal(row["quantity"]),
                "capital": _decimal(row["capital_rub"]),
                "confirmed_quantity": _decimal(row["confirmed_quantity"]),
            }
            for row in rows
        }

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        event_type: str,
        effective_date: str,
        shipment_id: str,
        supply_id: str,
        nm_id: int,
        stage_from: str,
        stage_to: str,
        quantity: Decimal,
        capital_rub: Decimal,
        confirmed_quantity: Decimal,
        cost_layer_id: str,
        warehouse: str,
        destination: str,
        payload: Mapping[str, Any],
        created_at: str,
        evidence_hash: str | None = None,
    ) -> None:
        event_payload = {
            "event_type": event_type,
            "effective_date": effective_date,
            "shipment_id": shipment_id,
            "supply_id": supply_id,
            "nm_id": nm_id,
            "stage_from": stage_from,
            "stage_to": stage_to,
            "quantity": _text_decimal(quantity),
            "capital_rub": _text_decimal(capital_rub),
            "confirmed_quantity": _text_decimal(confirmed_quantity),
            "cost_layer_id": cost_layer_id,
            "warehouse": warehouse,
            "destination": destination,
            "payload": dict(payload),
        }
        fingerprint = evidence_hash or _stable_hash(event_payload)
        existing = conn.execute(
            "SELECT evidence_hash FROM sheet_vitrina_v1_own_capital_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["evidence_hash"]) != fingerprint:
                raise ValueError(f"capital event {event_id} conflicts with persisted evidence")
            return
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_own_capital_events (
                event_id, event_type, effective_date, shipment_id, supply_id, nm_id,
                stage_from, stage_to, quantity, capital_rub, confirmed_quantity,
                cost_layer_id, warehouse, destination, payload_json, evidence_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                effective_date,
                shipment_id,
                supply_id,
                nm_id,
                stage_from,
                stage_to,
                _text_decimal(quantity),
                _text_decimal(capital_rub),
                _text_decimal(confirmed_quantity),
                cost_layer_id,
                str(warehouse or ""),
                str(destination or ""),
                _json_dumps(dict(payload)),
                fingerprint,
                created_at,
            ),
        )

    def _record_blocker(self, *, code: str, source_identity: str, details: Mapping[str, Any]) -> None:
        with _connect(self.runtime.db_path) as conn:
            _ensure_own_capital_schema(conn)
            fingerprint = _stable_hash({"code": code, "source_identity": source_identity, "details": dict(details)})
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_own_capital_blockers (
                    blocker_id, code, source_identity, details_json, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(blocker_id) DO NOTHING
                """,
                (f"blocker:{fingerprint}", code, source_identity, _json_dumps(dict(details)), self.timestamp_factory()),
            )
            conn.commit()


def _ensure_own_capital_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_payment_layers (
            payment_id TEXT PRIMARY KEY,
            shipment_id TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            invoice_total_cny TEXT NOT NULL,
            paid_cny TEXT NOT NULL,
            paid_rub TEXT NOT NULL,
            incremental_paid_share TEXT NOT NULL,
            cumulative_paid_share TEXT NOT NULL,
            stage TEXT NOT NULL,
            expenses_complete INTEGER NOT NULL DEFAULT 0,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_own_capital_payment_layers_by_shipment_date
        ON sheet_vitrina_v1_own_capital_payment_layers(shipment_id, effective_date, payment_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            shipment_id TEXT,
            supply_id TEXT,
            nm_id INTEGER NOT NULL,
            stage_from TEXT,
            stage_to TEXT NOT NULL,
            quantity TEXT NOT NULL,
            capital_rub TEXT NOT NULL,
            confirmed_quantity TEXT NOT NULL,
            cost_layer_id TEXT NOT NULL,
            warehouse TEXT,
            destination TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            evidence_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_own_capital_events_by_date_nm
        ON sheet_vitrina_v1_own_capital_events(effective_date, nm_id, event_id);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_own_capital_events_by_supply
        ON sheet_vitrina_v1_own_capital_events(supply_id, nm_id, event_type);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_wb_outstanding (
            original_supply_id TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            warehouse TEXT NOT NULL,
            destination TEXT NOT NULL,
            original_cost_layer_id TEXT NOT NULL,
            total_quantity TEXT NOT NULL,
            open_quantity TEXT NOT NULL,
            physical_total_quantity TEXT NOT NULL DEFAULT '0',
            physical_open_quantity TEXT NOT NULL DEFAULT '0',
            unit_cost_rub TEXT NOT NULL,
            writeoff_date TEXT NOT NULL,
            confirmed_share TEXT NOT NULL DEFAULT '0',
            final_acceptance_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(original_supply_id, nm_id)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_own_capital_wb_outstanding_fifo
        ON sheet_vitrina_v1_own_capital_wb_outstanding(warehouse, destination, nm_id, final_acceptance_date);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_daily_state (
            as_of_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            quantity TEXT NOT NULL,
            capital_rub TEXT NOT NULL,
            confirmed_quantity TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            calculated_at TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            PRIMARY KEY(as_of_date, nm_id, stage)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_own_capital_daily_state_by_date
        ON sheet_vitrina_v1_own_capital_daily_state(as_of_date, stage, nm_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_blockers (
            blocker_id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_own_capital_expense_certifications (
            shipment_id TEXT PRIMARY KEY,
            expenses_complete INTEGER NOT NULL DEFAULT 0,
            actor TEXT NOT NULL DEFAULT '',
            certified_at TEXT NOT NULL
        );
        """
    )
    outstanding_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(sheet_vitrina_v1_own_capital_wb_outstanding)").fetchall()
    }
    if "confirmed_share" not in outstanding_columns:
        conn.execute(
            "ALTER TABLE sheet_vitrina_v1_own_capital_wb_outstanding "
            "ADD COLUMN confirmed_share TEXT NOT NULL DEFAULT '0'"
        )
    added_physical_columns = False
    if "physical_total_quantity" not in outstanding_columns:
        conn.execute(
            "ALTER TABLE sheet_vitrina_v1_own_capital_wb_outstanding "
            "ADD COLUMN physical_total_quantity TEXT NOT NULL DEFAULT '0'"
        )
        added_physical_columns = True
    if "physical_open_quantity" not in outstanding_columns:
        conn.execute(
            "ALTER TABLE sheet_vitrina_v1_own_capital_wb_outstanding "
            "ADD COLUMN physical_open_quantity TEXT NOT NULL DEFAULT '0'"
        )
        added_physical_columns = True
    if added_physical_columns:
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_own_capital_wb_outstanding
            SET physical_total_quantity = total_quantity,
                physical_open_quantity = open_quantity
            WHERE physical_total_quantity = '0' AND total_quantity != '0'
            """
        )


def _expense_event_plans(
    document: Mapping[str, Any], expense_lines: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    document_id = str(document.get("document_id") or "").strip()
    document_type = str(document.get("document_type") or "")
    document_date = str(document.get("document_date") or "").strip()
    lines = [
        dict(item)
        for item in expense_lines
        if str(item.get("status") or "") not in {"excluded", "rejected"}
    ]
    if document_type == FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT:
        if str(document.get("parse_status") or "") != FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED:
            return []
        plans: list[dict[str, Any]] = []
        for line in lines:
            if str(line.get("currency") or "").upper() != "RUB":
                continue
            amount = _optional_decimal(line.get("amount_rub"))
            raw = _json_loads(line.get("raw"))
            raw_row = _json_loads(raw.get("row"))
            effective_date = str(
                raw_row.get("operation_date") or document_date
            ).strip()
            if amount is None or amount <= ZERO or not effective_date:
                continue
            plans.append(
                {
                    "event_document_id": (
                        f"financial_expense:{document_id}:"
                        f"{str(line.get('line_id') or '')}"
                    ),
                    "effective_date": _iso_date(
                        effective_date, "bank_fee_effective_date"
                    ),
                    "effective_date_source": (
                        "statement_row.operation_date"
                        if raw_row.get("operation_date")
                        else "document_date"
                    ),
                    "capital_rub": amount,
                    "component": "bank_fee_rub",
                    "expense_line_ids": [str(line.get("line_id") or "")],
                }
            )
        return plans
    if document_type not in {
        FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
        FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    }:
        return []
    if not document_date:
        return []
    eligible = [
        line
        for line in lines
        if document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE
        or bool(line.get("included_in_customs_total"))
    ]
    amounts = [
        amount
        for amount in (_optional_decimal(line.get("amount_rub")) for line in eligible)
        if amount is not None and amount > ZERO
    ]
    total = sum(amounts, ZERO)
    if total <= ZERO:
        return []
    return [
        {
            "event_document_id": f"financial_expense:{document_id}",
            "effective_date": _iso_date(document_date, "expense_effective_date"),
            "effective_date_source": "financial_document.document_date",
            "capital_rub": total,
            "component": (
                "logistics"
                if document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE
                else "customs_tax_vat"
            ),
            "expense_line_ids": [str(line.get("line_id") or "") for line in eligible],
        }
    ]


def _direct_rub_bank_fee_lines(
    expense_lines: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in expense_lines
        if str(item.get("status") or "") not in {"excluded", "rejected"}
        and str(item.get("currency") or "").upper() == "RUB"
    ]


def _validated_product_lines(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_line_ids: set[str] = set()
    for index, raw in enumerate(lines, start=1):
        line_id = _required_text(raw.get("line_id") or f"line-{index}", f"lines[{index}].line_id")
        if line_id in seen_line_ids:
            raise ValueError(f"duplicate supplier line_id: {line_id}")
        seen_line_ids.add(line_id)
        match_status = str(raw.get("match_status") or "matched").strip()
        nm_id = _positive_int(raw.get("nm_id") or raw.get("internal_nm_id"), f"lines[{index}].nm_id")
        qty = _positive_decimal(raw.get("qty"), f"lines[{index}].qty")
        amount = _optional_decimal(raw.get("amount"))
        if amount is None:
            amount = qty * _positive_decimal(raw.get("unit_price"), f"lines[{index}].unit_price")
        if amount <= ZERO or match_status not in {"matched", "matched_by_compatibility"}:
            raise ValueError(f"supplier line {line_id} is not deterministically matched with positive value")
        result.append(
            {
                "line_id": line_id,
                "nm_id": nm_id,
                "qty": _text_decimal(qty),
                "invoice_value_cny": _text_decimal(amount),
                "match_status": match_status,
            }
        )
    if not result:
        raise ValueError("supplier invoice has no product lines")
    return result


def _allocate_payment(
    lines: list[Mapping[str, Any]],
    *,
    paid_share: Decimal,
    paid_rub: Decimal,
) -> list[dict[str, Any]]:
    invoice_value = sum((_decimal(line["invoice_value_cny"]) for line in lines), ZERO)
    if invoice_value <= ZERO:
        raise ValueError("product invoice value is missing")
    allocated: list[dict[str, Any]] = []
    rub_remaining = paid_rub
    for index, line in enumerate(lines, start=1):
        value = _decimal(line["invoice_value_cny"])
        rub = rub_remaining if index == len(lines) else paid_rub * value / invoice_value
        rub_remaining -= rub
        allocated.append(
            {
                "line_id": str(line["line_id"]),
                "nm_id": int(line["nm_id"]),
                "paid_equivalent_qty": _decimal(line["qty"]) * paid_share,
                "allocated_rub": rub,
            }
        )
    return allocated


def _physical_stage_for_supplier_payment(
    *,
    effective_date: str,
    actual_shipment_date: str | None,
    actual_ff_acceptance_date: str | None,
) -> str:
    if actual_ff_acceptance_date and _iso_date(actual_ff_acceptance_date, "actual_ff_acceptance_date") <= effective_date:
        return STAGE_FF
    if actual_shipment_date and _iso_date(actual_shipment_date, "actual_shipment_date") <= effective_date:
        return STAGE_PRODUCTION_TO_FF
    return STAGE_PRODUCTION


def _apply_event(
    state: dict[int, dict[str, dict[str, Any]]],
    event: Mapping[str, Any],
    *,
    certifications: Mapping[str, bool] | None = None,
) -> None:
    nm_id = int(event["nm_id"])
    stage_to = _stage(event["stage_to"])
    stage_from = str(event.get("stage_from") or "")
    quantity = _decimal(event.get("quantity"))
    capital = _decimal(event.get("capital_rub"))
    confirmed = _decimal(event.get("confirmed_quantity"))
    payload = _json_loads(event.get("payload_json"))
    shipment_id = str(event.get("shipment_id") or "")
    certified = (certifications or {}).get(shipment_id) if shipment_id else None
    event_type = str(event.get("event_type") or "")
    if certified is not None and stage_to != STAGE_PRODUCTION and event_type != EVENT_COST_PAYMENT:
        if certified:
            confirmed = quantity
            payload["invalidate_confirmation"] = False
        else:
            confirmed = ZERO
            payload["invalidate_confirmation"] = True
    elif (
        shipment_id
        and event_type == EVENT_STAGE_TRANSFER
        and stage_to != STAGE_PRODUCTION
    ):
        if bool(payload.get("expenses_complete")):
            confirmed = quantity
            payload["invalidate_confirmation"] = False
        else:
            confirmed = ZERO
            payload["invalidate_confirmation"] = True
    stages = state.setdefault(nm_id, {})
    if stage_from:
        source = stages.setdefault(stage_from, _empty_bucket())
        if source["qty"] + Decimal("0.0000001") < quantity:
            raise ValueError(
                "persisted capital invariant violated: "
                f"event_id={event.get('event_id') or ''} "
                f"event_type={event_type} effective_date={event.get('effective_date') or ''} "
                f"{stage_from}->{stage_to} nmID {nm_id} has "
                f"{source['qty']} < {quantity}"
            )
        source_confirmed_out = (
            quantity * source["confirmed_qty"] / source["qty"]
            if source["qty"] > ZERO
            else ZERO
        )
        source["qty"] -= quantity
        source["capital"] -= capital
        source["confirmed_qty"] = max(ZERO, source["confirmed_qty"] - source_confirmed_out)
        if abs(source["capital"]) < MONEY_QUANT:
            source["capital"] = ZERO
        if abs(source["qty"]) < QTY_QUANT:
            source["qty"] = ZERO
            source["confirmed_qty"] = ZERO
    target = stages.setdefault(stage_to, _empty_bucket())
    target["qty"] += quantity
    target["capital"] += capital
    target["confirmed_qty"] += confirmed
    if event_type == EVENT_COST_PAYMENT:
        affected = min(target["qty"], _decimal(payload.get("affected_quantity")))
        cost_certified = certified if certified is not None else bool(payload.get("expenses_complete"))
        if cost_certified:
            target["confirmed_qty"] = min(target["qty"], target["confirmed_qty"] + affected)
        else:
            target["confirmed_qty"] = max(ZERO, target["confirmed_qty"] - affected)
    elif payload.get("invalidate_confirmation") and target["qty"] > ZERO:
        target["confirmed_qty"] = ZERO
    reason = str(payload.get("confirmation_reason") or "").strip()
    if reason and reason not in {"paid_purchase_cost", "complete_cost_chain", "expense_completeness_certified"}:
        target.setdefault("reasons", []).append(_reason_ru(reason))


def _empty_bucket() -> dict[str, Any]:
    return {"qty": ZERO, "capital": ZERO, "confirmed_qty": ZERO, "reasons": []}


def _component_reasons(raw: Any) -> list[str]:
    payload = _json_loads(raw)
    reasons: list[str] = []
    for key, value in payload.items():
        normalized = str(value or "")
        if normalized in {"missing", "pending", "needs_review", "estimated", "unknown"}:
            reasons.append(f"{key}: {normalized}")
    return reasons


def _validate_targeted_orphan_doprinato_idempotency_guard(
    *,
    reconciliation_supply_id: str,
    existing_events: Iterable[Any],
) -> None:
    if reconciliation_supply_id != TARGETED_ORPHAN_DOPRINATO_SUPPLY_ID:
        return
    rows = {str(row["event_id"]): row for row in existing_events}
    expected_ids = {
        TARGETED_ORPHAN_DOPRINATO_NORMAL_EVENT_ID,
        TARGETED_ORPHAN_DOPRINATO_EVENT_ID,
    }
    if set(rows) != expected_ids:
        raise ValueError(
            "targeted historical orphan Допринято idempotency guard mismatch: event_ids"
        )
    normal = rows[TARGETED_ORPHAN_DOPRINATO_NORMAL_EVENT_ID]
    orphan = rows[TARGETED_ORPHAN_DOPRINATO_EVENT_ID]
    orphan_payload = _json_loads(orphan["payload_json"])
    guards = {
        "normal_nm_id": int(normal["nm_id"]) == 391660889,
        "normal_quantity": _decimal(normal["quantity"]) == Decimal("1"),
        "orphan_nm_id": int(orphan["nm_id"])
        == TARGETED_ORPHAN_DOPRINATO_NM_ID,
        "orphan_quantity": _decimal(orphan["quantity"]) == ZERO,
        "orphan_capital": _decimal(orphan["capital_rub"]) == ZERO,
        "orphan_confirmed_quantity": _decimal(orphan["confirmed_quantity"])
        == ZERO,
        "orphan_reason": str(orphan_payload.get("classification") or "")
        == TARGETED_ORPHAN_DOPRINATO_REASON,
        "orphan_zero_capital": bool(orphan_payload.get("zero_capital")),
        "orphan_no_ff_writeoff": bool(orphan_payload.get("no_ff_writeoff")),
    }
    failed = sorted(name for name, passed in guards.items() if not passed)
    if failed:
        raise ValueError(
            "targeted historical orphan Допринято idempotency guard mismatch: "
            + ", ".join(failed)
        )


def _validate_targeted_orphan_doprinato_document_guard(
    *,
    reconciliation_supply_id: str,
    effective_date: str,
    requested: Mapping[int, Decimal],
    warehouse: str,
    original_supply_id: str | None,
) -> None:
    if reconciliation_supply_id != TARGETED_ORPHAN_DOPRINATO_SUPPLY_ID:
        return
    guards = {
        "effective_date": effective_date
        == TARGETED_ORPHAN_DOPRINATO_EFFECTIVE_DATE,
        "document_quantities": dict(requested)
        == TARGETED_ORPHAN_DOPRINATO_DOCUMENT_QUANTITIES,
        "warehouse": str(warehouse or "").strip()
        == TARGETED_ORPHAN_DOPRINATO_WAREHOUSE,
        "original_supply_id_absent": not str(original_supply_id or "").strip(),
    }
    failed = sorted(name for name, passed in guards.items() if not passed)
    if failed:
        raise ValueError(
            "targeted historical orphan Допринято document guard mismatch: "
            + ", ".join(failed)
        )


def _plan_targeted_orphan_doprinato_classification(
    *,
    reconciliation_supply_id: str,
    effective_date: str,
    requested: Mapping[int, Decimal],
    nm_id: int,
    quantity: Decimal,
    warehouse: str,
    original_supply_id: str | None,
    candidates: Iterable[Any],
) -> dict[str, Any] | None:
    if (
        reconciliation_supply_id != TARGETED_ORPHAN_DOPRINATO_SUPPLY_ID
        or nm_id != TARGETED_ORPHAN_DOPRINATO_NM_ID
    ):
        return None
    _validate_targeted_orphan_doprinato_document_guard(
        reconciliation_supply_id=reconciliation_supply_id,
        effective_date=effective_date,
        requested=requested,
        warehouse=warehouse,
        original_supply_id=original_supply_id,
    )
    candidate_rows = list(candidates)
    if not candidate_rows:
        raise ValueError(
            "targeted historical orphan Допринято guard mismatch: fifo_candidate_missing"
        )
    fifo_candidate = candidate_rows[0]
    guards = {
        "quantity": quantity == TARGETED_ORPHAN_DOPRINATO_QUANTITY,
        "fifo_candidate": str(fifo_candidate["original_supply_id"])
        == TARGETED_ORPHAN_DOPRINATO_FIFO_SUPPLY_ID,
        "tracked_outstanding": _decimal(fifo_candidate["open_quantity"]) == ZERO,
        "physical_outstanding": _decimal(
            fifo_candidate["physical_open_quantity"]
        )
        == ZERO,
        "paid_capital_outstanding": _decimal(fifo_candidate["open_quantity"])
        == ZERO,
    }
    failed = sorted(name for name, passed in guards.items() if not passed)
    if failed:
        raise ValueError(
            "targeted historical orphan Допринято guard mismatch: "
            + ", ".join(failed)
        )
    return {
        "event_id": TARGETED_ORPHAN_DOPRINATO_EVENT_ID,
        "reason": TARGETED_ORPHAN_DOPRINATO_REASON,
        "nm_id": nm_id,
        "requested_quantity": quantity,
        "fifo_candidate_supply_id": str(fifo_candidate["original_supply_id"]),
    }


def _reason_ru(reason: str) -> str:
    return {
        "expense_completeness_not_certified": "полнота расходов не подтверждена",
        "payment_date_requires_confirmation": "payment date требует подтверждения",
        "historical_date_not_confirmed": "историческая дата не подтверждена",
    }.get(reason, reason)


def _date_range(start: str, end: str) -> Iterable[str]:
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def _stage(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized not in OWN_PRODUCT_CAPITAL_STAGES:
        raise ValueError(f"unsupported product capital stage: {normalized}")
    return normalized


def _required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid Decimal value: {value!r}") from exc


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return _decimal(value)


def _positive_decimal(value: Any, field: str) -> Decimal:
    normalized = _decimal(value)
    if normalized <= ZERO:
        raise ValueError(f"{field} must be > 0")
    return normalized


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    normalized = _decimal(value)
    if normalized < ZERO:
        raise ValueError(f"{field} must be >= 0")
    return normalized


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return None if denominator == ZERO else numerator / denominator


def _text_decimal(value: Decimal) -> str:
    normalized = value.quantize(MONEY_QUANT)
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _iso_date(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _literal_like_prefix(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        + "%"
    )


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
