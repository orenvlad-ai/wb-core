#!/usr/bin/env python3
"""Safety smoke for exact multi-invoice warehouse/cost queue replay."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_cost_queue_replay import (  # noqa: E402
    _checkpoint_audit,
    _ensure_audit_schema,
    _load_audit_record,
    _mark_audit_failed,
    _start_audit,
    apply_plan,
    build_plan,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalBlock,
    WarehouseFunctionalError,
)


def main() -> int:
    with TemporaryDirectory(prefix="warehouse-cost-queue-replay-") as temp:
        runtime_dir = Path(temp) / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _fixture(runtime.db_path)
        before = _sidecars(runtime.db_path)
        plan = build_plan(
            runtime,
            invoice_numbers=["26GN582", "26GN583"],
        )
        after = _sidecars(runtime.db_path)
        _assert(before == after, "query-only plan does not change SQLite sidecars")
        _assert(plan["would_change"], "pending exact queues require replay")
        _assert(
            plan["performance"]["query_only"] == 1
            and plan["performance"]["sqlite_total_changes"] == 0,
            "dry-run is SQLite query-only",
        )
        _assert(
            len(plan["targeted_recalc_requests"]) == 2
            and plan["scope"]["affected_nm_ids"] == [101, 102],
            "two exact revisions form the bounded SKU closure",
        )
        _assert(
            plan["targets"][0]["commission"]["expense_total_rub"]
            == plan["targets"][0]["capital"]["total_rub"],
            "commission expense and capital totals reconcile",
        )
        _ensure_audit_schema(runtime)
        _start_audit(runtime, plan)
        _checkpoint_audit(
            runtime,
            plan,
            {"functional_plan": {"plan_fingerprint": "sha256:functional"}},
        )
        _mark_audit_failed(
            runtime,
            plan,
            {"functional_plan": {"plan_fingerprint": "sha256:functional"}},
            RuntimeError("injected interruption"),
        )
        failed = _load_audit_record(runtime, plan["fingerprint"]) or {}
        _assert(
            failed.get("status") == "failed"
            and failed["steps"]["functional_plan"]["plan_fingerprint"]
            == "sha256:functional",
            "durable audit preserves the pre-write checkpoint",
        )
        complete_plan = json.loads(json.dumps(plan))
        complete_plan["would_change"] = False
        complete_plan["fingerprint"] = _replay_fingerprint(complete_plan)
        result = apply_plan(runtime, complete_plan, actor="smoke")
        _assert(
            result["idempotent"] and not result["applied"],
            "reviewed no-op does not write",
        )
    _assert_exact_queue_filter()
    print("warehouse_cost_queue_replay_smoke: ok")
    return 0


def _fixture(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_supplier_shipments(
                shipment_id TEXT PRIMARY KEY,invoice_no TEXT,shipment_date TEXT,
                actual_shipment_date TEXT,actual_ff_acceptance_date TEXT,
                order_status TEXT,expenses_complete INTEGER,updated_at TEXT,
                archived_at TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines(
                line_id TEXT PRIMARY KEY,shipment_id TEXT,line_type TEXT,
                sort_order INTEGER,internal_nm_id INTEGER,qty REAL,
                unit_price REAL,amount REAL,currency TEXT,match_status TEXT,
                raw_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_financial_documents(
                document_id TEXT PRIMARY KEY,supplier_order_id TEXT,
                document_type TEXT,file_sha256 TEXT,parse_status TEXT,
                document_date TEXT,total_amount_rub REAL,updated_at TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_financial_expense_lines(
                line_id TEXT PRIMARY KEY,supplier_order_id TEXT,
                financial_document_id TEXT,sort_order INTEGER,category TEXT,
                stage TEXT,description TEXT,amount REAL,currency TEXT,
                amount_rub REAL,vat_amount_rub REAL,status TEXT,raw_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_own_capital_events(
                event_id TEXT PRIMARY KEY,event_type TEXT,effective_date TEXT,
                shipment_id TEXT,nm_id INTEGER,quantity TEXT,capital_rub TEXT,
                evidence_hash TEXT,payload_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_targeted_recalc_queue(
                queue_id TEXT PRIMARY KEY,stable_source_id TEXT,
                source_revision TEXT,effective_date TEXT,
                affected_nm_ids_json TEXT,status TEXT,requested_at TEXT,
                started_at TEXT,finished_at TEXT,error TEXT
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(
                version_id TEXT PRIMARY KEY,version_kind TEXT,effective_at TEXT,
                plan_fingerprint TEXT,local_source_digest TEXT,created_at TEXT
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(
                slot INTEGER PRIMARY KEY,version_id TEXT
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_supplier_cost_states(
                version_id TEXT,shipment_id TEXT,source_fingerprint TEXT,
                calculation_fingerprint TEXT,expenses_complete INTEGER,
                calculation_available INTEGER
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(
                version_id TEXT,warehouse_key TEXT,nm_id INTEGER,quantity TEXT,
                wac_rub TEXT,capital_rub TEXT,cost_covered_quantity TEXT,
                quality TEXT,certified INTEGER,wb_quantity TEXT,
                wb_in_way_to_client TEXT,wb_in_way_from_client TEXT,
                provenance_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_ready_snapshots(
                bundle_version TEXT,as_of_date TEXT,plan_json TEXT,
                refreshed_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES(?,?,?,?,?,?)",
            (
                "whfv-before",
                "hourly_wb_sync",
                "2026-07-25T18:15:28Z",
                "sha256:before",
                "sha256:local",
                "2026-07-25T18:15:29Z",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'whfv-before')"
        )
        for index, (invoice, nm_id, amount, day) in enumerate(
            (
                ("26GN582", 101, "13525.89", "2026-07-16"),
                ("26GN583", 102, "18713.34", "2026-07-20"),
            ),
            start=1,
        ):
            shipment_id = f"shipment-{index}"
            document_id = f"document-{index}"
            queue_id = f"queue-{index}"
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_supplier_shipments VALUES(?,?,?,?,?,?,?,?,NULL)",
                (
                    shipment_id,
                    invoice,
                    "2026-08-01",
                    None,
                    None,
                    "production",
                    0,
                    "2026-07-26T06:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_supplier_shipment_lines VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"line-{index}",
                    shipment_id,
                    "product",
                    1,
                    nm_id,
                    10,
                    1,
                    10,
                    "CNY",
                    "matched",
                    "{}",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES(?,?,?,?,?,?,?,?)",
                (
                    document_id,
                    shipment_id,
                    "bank_fee_statement",
                    "a" * 64,
                    "confirmed",
                    day,
                    amount,
                    "2026-07-26T06:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"expense-{index}",
                    shipment_id,
                    document_id,
                    1,
                    "bank_transfer_fee",
                    "production",
                    "fee",
                    amount,
                    "RUB",
                    amount,
                    0,
                    "confirmed",
                    "{}",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_own_capital_events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"capital-{index}",
                    "cost_payment",
                    day,
                    shipment_id,
                    nm_id,
                    "0",
                    amount,
                    f"sha256:capital-{index}",
                    json.dumps(
                        {
                            "provenance": {
                                "financial_document_id": document_id
                            }
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)",
                (
                    queue_id,
                    f"supplier_costs:{shipment_id}",
                    f"sha256:revision-{index}",
                    "2026-07-24",
                    json.dumps([nm_id]),
                    f"2026-07-26T06:1{index}:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states VALUES(?,?,?,?,?,?)",
                (
                    "whfv-before",
                    shipment_id,
                    f"sha256:old-source-{index}",
                    f"sha256:old-calculation-{index}",
                    0,
                    1,
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "whfv-before",
                    "production",
                    nm_id,
                    "10",
                    "1",
                    "10",
                    "10",
                    "provisional",
                    0,
                    "0",
                    "0",
                    "0",
                    "{}",
                ),
            )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)",
            (
                "bundle",
                "2026-07-26",
                json.dumps(
                    {
                        "date_columns": ["2026-07-16", "2026-07-20"],
                        "sheets": [],
                    }
                ),
                "2026-07-26T06:00:00Z",
            ),
        )
        conn.commit()


def _assert_exact_queue_filter() -> None:
    block = WarehouseFunctionalBlock.__new__(WarehouseFunctionalBlock)
    block.readback = lambda: {"status": "ready"}  # type: ignore[method-assign]
    block._last_good_wb_payload = lambda: {}  # type: ignore[method-assign]
    selected = [
        _request("queue-1", "supplier_costs:shipment-1", 101),
        _request("queue-2", "supplier_costs:shipment-2", 102),
    ]
    disjoint = _request("queue-3", "supplier_costs:other", 999)
    block._build_plan = lambda **_: {  # type: ignore[method-assign]
        "diff": {"lines": []},
        "targeted_recalc_requests": [*selected, disjoint],
    }
    plan = block.build_targeted_recovery_plan(
        affected_nm_ids=[101, 102],
        stable_source_ids=[
            "supplier_costs:shipment-1",
            "supplier_costs:shipment-2",
        ],
        targeted_recalc_requests=selected,
    )
    _assert(
        [item["queue_id"] for item in plan["targeted_recalc_requests"]]
        == ["queue-1", "queue-2"],
        "functional apply completes only exact selected queues",
    )
    overlapping = _request("queue-4", "supplier_costs:overlap", 102)
    block._build_plan = lambda **_: {  # type: ignore[method-assign]
        "diff": {"lines": []},
        "targeted_recalc_requests": [*selected, overlapping],
    }
    try:
        block.build_targeted_recovery_plan(
            affected_nm_ids=[101, 102],
            stable_source_ids=[
                "supplier_costs:shipment-1",
                "supplier_costs:shipment-2",
            ],
            targeted_recalc_requests=selected,
        )
    except WarehouseFunctionalError as exc:
        _assert(
            "overlaps" in str(exc),
            "overlapping non-target queue fails closed",
        )
    else:
        raise AssertionError("overlapping non-target queue was accepted")


def _request(queue_id: str, stable_source_id: str, nm_id: int) -> dict:
    return {
        "queue_id": queue_id,
        "stable_source_id": stable_source_id,
        "source_revision": f"sha256:{queue_id}",
        "effective_date": "2026-07-24",
        "affected_nm_ids": [nm_id],
    }


def _replay_fingerprint(plan: dict) -> str:
    from apps.warehouse_cost_queue_replay import (
        _fingerprint,
        _fingerprint_material,
    )

    return _fingerprint(_fingerprint_material(plan))


def _sidecars(path: Path) -> list[tuple[str, int]]:
    return sorted(
        (candidate.name, candidate.stat().st_size)
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
        )
        if candidate.exists()
    )


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
