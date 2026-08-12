#!/usr/bin/env python3
"""Checks canonical exact/stage cost presentation states."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _build_supplier_payment_checklist,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    _exact_landed_cost_cell,
    _functional_stage_cost_cell,
    build_supplier_financial_readiness,
    build_supplier_payment_readiness,
)
from packages.application.warehouse_functional import (  # noqa: E402
    _supplier_allocation_with_certification,
    ensure_warehouse_functional_schema,
    load_supplier_flow_cost_state,
)


SHIPMENT_ID = "stage-cost-smoke"
VERSION_ID = "whfv_stage_cost_smoke"


def main() -> int:
    _assert_supplier_financial_readiness_contract()
    with TemporaryDirectory(prefix="supplier-cost-status-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed(runtime)
        proof = {
            "certification": {
                "certified": True,
                "active_version_id": VERSION_ID,
            }
        }
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=proof,
        ):
            queued = load_supplier_flow_cost_state(
                runtime=runtime, shipment_id=SHIPMENT_ID
            )
        _assert(
            queued["china_to_ff"]["status"] == "queued"
            and queued["china_to_ff"]["average_unit_cost_rub"] is None,
            "queued replay hides stale 92.95 value",
        )
        queued_cell = _functional_stage_cost_cell(
            {"summary": {"functional_stage_costs": queued}}, "china_to_ff"
        )
        _assert(
            queued_cell["display"] == "Ожидает пересчёта"
            and queued_cell["value"] is None,
            "queued stage has explicit waiting text",
        )
        _set_queue(runtime.db_path, "error", "source fingerprint mismatch")
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=proof,
        ):
            failed = load_supplier_flow_cost_state(
                runtime=runtime, shipment_id=SHIPMENT_ID
            )
        _assert(
            failed["china_to_ff"]["status"] == "error"
            and "fingerprint" in failed["china_to_ff"]["blocker"],
            "failed replay exposes exact blocker",
        )
        _set_queue(runtime.db_path, "running", "")
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=proof,
        ):
            running = load_supplier_flow_cost_state(
                runtime=runtime, shipment_id=SHIPMENT_ID
            )
        _assert(
            running["china_to_ff"]["status"] == "running"
            and running["china_to_ff"]["average_unit_cost_rub"] is None,
            "running replay also hides the frozen value",
        )
        _set_queue(runtime.db_path, "complete", "")
        _set_balance_cost(runtime.db_path, "107.79")
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=proof,
        ):
            certified = load_supplier_flow_cost_state(
                runtime=runtime, shipment_id=SHIPMENT_ID
            )
        _assert(
            certified["china_to_ff"]["status"] == "certified"
            and certified["china_to_ff"]["average_unit_cost_rub"] == "107.79",
            "successful replay replaces stale 92.95 with current canonical value",
        )
        stale_cell = _functional_stage_cost_cell(
            {
                "summary": {
                    "functional_stage_costs": {
                        "china_to_ff": {
                            "status": "stale",
                            "average_unit_cost_rub": "92.95",
                            "blocker": "source/calculation fingerprints differ",
                        }
                    }
                }
            },
            "china_to_ff",
        )
        _assert(
            stale_cell["value"] is None
            and "fingerprints" in stale_cell["display"],
            "stale numeric value is suppressed even when persisted",
        )
        _set_acceptance(runtime, "2026-07-21")
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=proof,
        ):
            exited = load_supplier_flow_cost_state(
                runtime=runtime, shipment_id=SHIPMENT_ID
            )
        exited_cell = _functional_stage_cost_cell(
            {"summary": {"functional_stage_costs": exited}}, "china_to_ff"
        )
        _assert(
            exited_cell["display"] == "Не применяется: поставка уже на ФФ",
            "exited stage is neutral and explained",
        )
        certified_exact = _exact_landed_cost_cell(
            {
                "summary": {
                    "per_unit": {
                        "exact_landed_cost_per_unit_rub": 113.42,
                        "exact_cost_status": "certified",
                    }
                }
            }
        )
        provisional_exact = _exact_landed_cost_cell(
            {
                "header": {"expenses_complete": True},
                "summary": {
                    "per_unit": {
                        "exact_landed_cost_per_unit_rub": 107.79,
                        "exact_cost_status": "provisional",
                    }
                },
            }
        )
        unavailable_exact = _exact_landed_cost_cell(
            {
                "summary": {
                    "per_unit": {
                        "exact_landed_cost_per_unit_rub": 111.18,
                        "exact_cost_status": "unavailable",
                        "exact_cost_blockers": ["Нет current fingerprint"],
                    }
                }
            }
        )
        _assert(certified_exact["status"] == "certified", "certified exact is green")
        _assert(
            provisional_exact["status"] == "provisional",
            "expenses_complete alone cannot make exact green",
        )
        _assert(
            provisional_exact["note"]
            == "Предварительно: active functional version ещё не сертифицирована."
            and "Ожидается" not in provisional_exact["note"],
            "completed provisional cost never reports stale replay waiting",
        )
        _assert(
            unavailable_exact["value"] is None
            and unavailable_exact["status"] == "unavailable",
            "unavailable exact never exposes a stale number",
        )
    print("supplier_cost_status_smoke: ok")
    return 0


def _assert_supplier_financial_readiness_contract() -> None:
    compatible_existing = _supplier_allocation_with_certification(
        {
            "source_fingerprint": "current-contract",
            "compatible_source_fingerprints": [
                "legacy-expenses-false",
                "legacy-expenses-true",
            ],
            "calculation_fingerprint": "calculation",
            "financial_readiness": {"ready": True},
            "document_controls": [
                {
                    "conserved": True,
                    "eligible_component_count": 1,
                    "allocated_component_count": 1,
                }
            ],
        },
        active_version_id="legacy-version",
        active_fingerprints=("legacy-expenses-true", "calculation"),
    )
    _assert(
        compatible_existing["certification"]["certified"] is True,
        "existing shipment certification survives derived legacy expenses-complete fingerprint compatibility",
    )
    shipment = {"invoice_amount_total": 100, "currency": "CNY"}
    first = {
        "document_id": "payment-1",
        "document_type": "supplier_cny_payment",
        "status": "posted",
        "cny_amount": 40,
    }
    partial = build_supplier_payment_readiness(shipment, [first, dict(first)])
    _assert(
        partial["status"] == "partial"
        and partial["confirmed_paid"] == 40.0
        and partial["remaining"] == 60.0
        and partial["confirmed_document_count"] == 1,
        "canonical payment readiness aggregates without duplicate documents",
    )
    partial_rows = _build_supplier_payment_checklist(
        payment_readiness=partial,
        payment_documents=[
            {
                "document_id": "payment-1",
                "document_type": "supplier_cny_payment",
                "is_uploaded": True,
                "status": "posted",
            },
            {
                "document_id": "payment-1",
                "document_type": "supplier_cny_payment",
                "is_uploaded": True,
                "status": "posted",
            },
        ],
    )
    _assert(
        len(partial_rows) == 2
        and sum(1 for item in partial_rows if not item.get("is_uploaded")) == 1,
        "partial payment checklist has confirmed rows plus exactly one missing remainder",
    )
    complete = build_supplier_payment_readiness(
        shipment,
        [
            first,
            {
                "document_id": "payment-2",
                "document_type": "supplier_cny_payment",
                "status": "posted",
                "cny_amount": 60,
            },
        ],
    )
    _assert(complete["complete"] is True and complete["status"] == "full", "100% payment is green-ready")
    _assert(
        all(item.get("is_uploaded") for item in _build_supplier_payment_checklist(
            payment_readiness=complete,
            payment_documents=[
                {
                    "document_id": "payment-1",
                    "document_type": "supplier_cny_payment",
                    "is_uploaded": True,
                    "status": "posted",
                }
            ],
        )),
        "100% payment checklist has no synthetic missing row",
    )
    financial = build_supplier_financial_readiness(
        payment_readiness=complete,
        financial_documents=[
            {"document_type": "logistics_invoice", "parse_status": "parsed"},
            {"document_type": "customs_declaration", "parse_status": "confirmed"},
            {"document_type": "bank_fee_statement", "parse_status": "parsed"},
            {"document_type": "packing_list", "parse_status": "needs_review"},
            {"document_type": "bank_control_statement", "parse_status": "needs_review"},
        ],
        document_controls=[
            {"cost_affecting": True, "conserved": True},
        ],
    )
    _assert(
        financial["ready"] is True
        and set(financial["excluded_informational_types"])
        == {"packing_list", "bank_control_statement"},
        "packing list and bank-control statement do not hold exact-cost readiness yellow",
    )
    pending = build_supplier_financial_readiness(
        payment_readiness=partial,
        financial_documents=[
            {"document_type": "logistics_invoice", "parse_status": "parsed"},
            {"document_type": "customs_declaration", "parse_status": "parsed"},
            {"document_type": "bank_fee_statement", "parse_status": "parsed"},
        ],
        document_controls=[{"cost_affecting": True, "conserved": True}],
    )
    _assert(
        pending["ready"] is False
        and any(item["code"] == "supplier_payment_incomplete" for item in pending["blockers"]),
        "partial supplier payment keeps exact cost yellow",
    )


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": SHIPMENT_ID,
            "created_at": "2026-07-24T09:00:00Z",
            "updated_at": "2026-07-24T09:00:00Z",
            "shipment_date": "2026-06-15",
            "actual_shipment_date": "2026-06-25",
            "actual_ff_acceptance_date": "",
            "order_status": "in_transit",
            "invoice_no": "26GN462",
            "invoice_date": "2026-06-15",
        },
        lines=[],
    )
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_active(
                slot,version_id,updated_at
            ) VALUES(1,?,?)
            """,
            (VERSION_ID, "2026-07-24T09:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                VERSION_ID,
                "china_to_ff",
                123,
                "100",
                "92.95",
                "9295",
                "100",
                "certified",
                1,
                "0",
                "0",
                "0",
                json.dumps(
                    {
                        "source_records": [
                            {
                                "shipment_id": SHIPMENT_ID,
                                "flow_quantity": "100",
                                "flow_capital_rub": "9295",
                                "quality": "certified",
                                "expenses_complete_certification": True,
                            }
                        ]
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue(
                queue_id,stable_source_id,source_revision,effective_date,
                affected_nm_ids_json,status,requested_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "queue-stage-smoke",
                f"supplier_shipment:{SHIPMENT_ID}",
                "sha256:current",
                "2026-06-25",
                "[123]",
                "queued",
                "2026-07-24T09:01:00Z",
            ),
        )
        conn.commit()


def _set_queue(db_path: Path, status: str, error: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue
            SET status=?,error=? WHERE queue_id='queue-stage-smoke'
            """,
            (status, error or None),
        )
        conn.commit()


def _set_balance_cost(db_path: Path, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT provenance_json
            FROM sheet_vitrina_v1_warehouse_functional_balances
            WHERE version_id=? AND warehouse_key='china_to_ff'
            """,
            (VERSION_ID,),
        ).fetchone()
        provenance = json.loads(str(row[0])) if row is not None else {}
        for source in provenance.get("source_records") or []:
            source["flow_capital_rub"] = str(float(value) * 100)
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_warehouse_functional_balances
            SET wac_rub=?,capital_rub=CAST(quantity AS REAL)*?,provenance_json=?
            WHERE version_id=? AND warehouse_key='china_to_ff'
            """,
            (
                value,
                value,
                json.dumps(provenance),
                VERSION_ID,
            ),
        )
        conn.commit()


def _set_acceptance(runtime: RegistryUploadDbBackedRuntime, value: str) -> None:
    shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    runtime.save_supplier_shipment(
        header={
            **shipment["header"],
            "actual_ff_acceptance_date": value,
            "order_status": "accepted_ff",
            "updated_at": "2026-07-24T09:02:00Z",
        },
        lines=shipment["lines"],
    )


def _assert(value: object, label: str) -> None:
    if not value:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
