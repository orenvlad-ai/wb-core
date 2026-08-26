#!/usr/bin/env python3
"""Bounded recovery for supplier shipment 26GN390 and logistics invoice 136."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.root_storage_policy import (  # noqa: E402
    admit_root_write,
    predict_sqlite_backup_bytes,
)
from packages.application.own_product_capital import (  # noqa: E402
    OwnProductCapitalBlock,
)
from packages.application.our_wb_costs import OurWbCostBlock  # noqa: E402
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalBlock,
    _functional_local_source_view,
    _source_rows,
    _supplier_cost_allocations,
    enqueue_warehouse_targeted_recalculation,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    _sqlite_backup as create_verified_sqlite_backup,
    restore_verified_supplier_backup,
)


SHIPMENT_ID = "sup_b3070385b00b4eb680bd805d751d65be"
INVOICE_NO = "26GN390"
DOCUMENT_NUMBER = "136"
ACTIVE_DOCUMENT_ID = "fdoc_b53824642d3e41bd8afa60785bdb6ed2"
ARCHIVED_DOCUMENT_ID = "fdoc_883d0528332c4900aa92348a45163b48"
EXPECTED_FILE_SHA256 = (
    "e9358919df6b1de9ebb75943e2d2d05dc2a522df8c16328c05559a07c3837136"
)
EXPECTED_AMOUNT_RUB = "1075030.00"
EXPECTED_STALE_FF_CAPITAL_RUB = "10177161.12"
EXPECTED_ACTIVE_FF_CAPITAL_RUB = "9102131.12"
TARGET_FF_ACCEPTANCE_DATE = "2026-07-21"
TARGET_DOCUMENT_IDS = {ACTIVE_DOCUMENT_ID, ARCHIVED_DOCUMENT_ID}
AUDIT_TABLE = "sheet_vitrina_v1_supplier_26gn390_recovery_audit"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    report = build_plan(runtime.db_path)
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if str(args.fingerprint or "") != str(report["fingerprint"]):
        raise ValueError("apply requires the exact current dry-run fingerprint")
    backup_root = (
        Path(args.backup_dir)
        if args.backup_dir
        else runtime.runtime_dir / "backups" / "supplier-26gn390-recovery"
    )
    result = apply_plan(runtime, report, backup_root=backup_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_plan(db_path: Path) -> dict[str, Any]:
    planned_at = _stable_business_timestamp()
    with _connect(db_path, read_only=True) as conn:
        shipment = _shipment(conn)
        documents = _documents(conn)
        side_effects = _side_effects(conn)
        functional_fingerprints = _supplier_functional_fingerprint_projection(
            conn,
            recovery_end_date=planned_at[:10],
        )
        side_effects["functional_fingerprints"] = functional_fingerprints
        target_digest = _target_digest(shipment, documents)
        non_target_digest = _non_target_digest(conn)
    _validate_identity(shipment, documents)
    active = next(
        item for item in documents if item["document_id"] == ACTIVE_DOCUMENT_ID
    )
    archive = next(
        item for item in documents if item["document_id"] == ARCHIVED_DOCUMENT_ID
    )
    invoice_136_documents = [
        item for item in documents if _is_invoice_136(item)
    ]
    if {str(item["document_id"]) for item in invoice_136_documents} != (
        TARGET_DOCUMENT_IDS
    ):
        raise ValueError(
            "approved recovery requires exactly the diagnosed two invoice-136 documents"
        )
    if (
        str(active["parse_status"]) == "excluded"
        or str(archive["parse_status"]) != "excluded"
    ):
        raise ValueError(
            "approved recovery requires one active and the diagnosed archived invoice-136 document"
        )
    extra_active = [
        item
        for item in documents
        if item["document_id"] not in TARGET_DOCUMENT_IDS
        and _is_invoice_136(item)
        and str(item["parse_status"]) != "excluded"
    ]
    if extra_active:
        raise ValueError("unexpected active invoice-136 duplicate blocks recovery")
    actual_ff_acceptance_date = str(
        shipment.get("actual_ff_acceptance_date") or ""
    )
    if actual_ff_acceptance_date != TARGET_FF_ACCEPTANCE_DATE:
        raise ValueError(
            "target shipment FF acceptance date differs from the approved current truth"
        )
    if not bool(shipment.get("expenses_complete")):
        raise ValueError("26GN390 expenses_complete must already be true")
    changes: list[dict[str, Any]] = []
    if (
        int(side_effects.get("archived_financial_capital_event_count") or 0) > 0
        or int(side_effects.get("active_financial_capital_event_count") or 0) == 0
    ):
        changes.append(
            {
                "action": "reconcile_financial_capital_chain",
                "active_document_id": ACTIVE_DOCUMENT_ID,
                "archived_document_id": ARCHIVED_DOCUMENT_ID,
            }
        )
    current_ff_capital = _money(
        side_effects.get("current_ff_cost_layer_capital_rub")
    )
    if current_ff_capital not in {
        EXPECTED_STALE_FF_CAPITAL_RUB,
        EXPECTED_ACTIVE_FF_CAPITAL_RUB,
    }:
        raise ValueError(
            "current supplier FF capital differs from the approved bounded target: "
            + current_ff_capital
        )
    if current_ff_capital == EXPECTED_STALE_FF_CAPITAL_RUB:
        changes.append(
            {
                "action": "rebuild_supplier_costs",
                "shipment_id": SHIPMENT_ID,
                "old_capital_rub": EXPECTED_STALE_FF_CAPITAL_RUB,
                "new_capital_rub": EXPECTED_ACTIVE_FF_CAPITAL_RUB,
                "removed_archived_amount_rub": EXPECTED_AMOUNT_RUB,
            }
        )
    if not bool(functional_fingerprints.get("matches_active_version")):
        changes.append(
            {
                "action": "publish_functional_source_revision",
                "active_version_id": functional_fingerprints.get(
                    "active_version_id"
                ),
                "active_source_fingerprint": functional_fingerprints.get(
                    "active_source_fingerprint"
                ),
                "active_calculation_fingerprint": functional_fingerprints.get(
                    "active_calculation_fingerprint"
                ),
                "current_source_fingerprint": functional_fingerprints.get(
                    "current_source_fingerprint"
                ),
                "current_calculation_fingerprint": functional_fingerprints.get(
                    "current_calculation_fingerprint"
                ),
            }
        )
    source_revision = "sha256:" + _hash(
        {
            "shipment_id": SHIPMENT_ID,
            "target_digest": target_digest,
            "active_document_id": ACTIVE_DOCUMENT_ID,
            "archived_document_id": ARCHIVED_DOCUMENT_ID,
            "current_source_fingerprint": functional_fingerprints.get(
                "current_source_fingerprint"
            ),
            "current_calculation_fingerprint": functional_fingerprints.get(
                "current_calculation_fingerprint"
            ),
        }
    )
    candidate = (
        _candidate_recovery_projection(
            db_path,
            planned_at=planned_at,
            source_revision=source_revision,
            reconcile_capital=any(
                item["action"] == "reconcile_financial_capital_chain"
                for item in changes
            ),
            rebuild_supplier_costs=any(
                item["action"] == "rebuild_supplier_costs"
                for item in changes
            ),
        )
        if changes
        else None
    )
    plan_material = {
        "contract_name": "supplier_26gn390_recovery_plan_v1",
        "scope": {
            "shipment_id": SHIPMENT_ID,
            "invoice_no": INVOICE_NO,
            "document_number": DOCUMENT_NUMBER,
            "active_document_id": ACTIVE_DOCUMENT_ID,
            "archived_document_id": ARCHIVED_DOCUMENT_ID,
            "file_sha256": EXPECTED_FILE_SHA256,
            "amount_rub": EXPECTED_AMOUNT_RUB,
        },
        "target_before_digest": target_digest,
        "target_derived_before": side_effects,
        "non_target_before_digest": non_target_digest,
        "source_revision": source_revision,
        "planned_at": planned_at,
        "expected_affected_rows": len(changes),
        "changes": changes,
        "candidate": candidate,
    }
    fingerprint = "sha256:" + _hash(plan_material)
    return {
        **plan_material,
        "mode": "dry_run",
        "would_change": bool(changes),
        "fingerprint": fingerprint,
        "readback": _readback_projection(shipment, documents, side_effects),
    }


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    raise ValueError(
        "legacy supplier 26GN390 mutation is disabled; use the current "
        "warehouse cost queue replay with an exact reviewed fingerprint"
    )

    # Historical implementation retained below as migration evidence only.
    if not plan.get("would_change"):
        return {
            **dict(plan),
            "mode": "apply",
            "applied": False,
            "backup": None,
            "post_apply": {"idempotent": True, "changed_rows": 0},
        }
    with warehouse_functional_write_lock(runtime.runtime_dir):
        return _apply_plan_locked(runtime, plan, backup_root=backup_root)


def _apply_plan_locked(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    current = build_plan(runtime.db_path)
    if str(current.get("fingerprint") or "") != str(
        plan.get("fingerprint") or ""
    ):
        raise ValueError("26GN390 source or candidate changed after dry-run")
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / f"registry_upload.26gn390.{timestamp}.sqlite3"
    backup = create_verified_sqlite_backup(runtime.db_path, backup_path)
    applied_at = str(plan.get("planned_at") or "")
    capital_reconciliation: dict[str, Any] | None = None
    queue: dict[str, Any] | None = None
    supplier_cost_rebuild: dict[str, Any] | None = None
    functional_publication: dict[str, Any] | None = None
    try:
        shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
        if any(
            item["action"] == "reconcile_financial_capital_chain"
            for item in plan.get("changes") or []
        ):
            capital = OwnProductCapitalBlock(
                runtime=runtime,
                timestamp_factory=lambda: applied_at,
            )
            removed = capital.remove_financial_document_expenses(
                ARCHIVED_DOCUMENT_ID,
                recalculate=False,
            )
            materialized = capital.materialize_persisted_expense_events(
                shipment_id=SHIPMENT_ID,
            )
            capital_reconciliation = {
                "archived_events_removed": int(
                    removed.get("removed_event_count") or 0
                ),
                "active_chain": materialized,
            }
        nm_ids = sorted(
            {
                int(item.get("internal_nm_id") or 0)
                for item in shipment.get("lines") or []
                if int(item.get("internal_nm_id") or 0) > 0
            }
        )
        queue = enqueue_warehouse_targeted_recalculation(
            runtime=runtime,
            stable_source_id=f"supplier_shipment:{SHIPMENT_ID}",
            source_revision=str(plan["source_revision"]),
            effective_date=str(
                (shipment.get("header") or {}).get("actual_shipment_date")
                or (shipment.get("header") or {}).get("invoice_date")
                or applied_at[:10]
            )[:10],
            affected_nm_ids=nm_ids,
            requested_at=applied_at,
        )
        if any(
            item["action"] == "rebuild_supplier_costs"
            for item in plan.get("changes") or []
        ):
            supplier_cost_rebuild = OurWbCostBlock(
                runtime=runtime,
                timestamp_factory=lambda: applied_at,
            ).materialize_supplier_ff_cost_layer(SHIPMENT_ID)
        functional = WarehouseFunctionalBlock(
            runtime=runtime,
            timestamp_factory=lambda: applied_at,
        )
        functional_plan = functional.build_emergency_rebuild_plan()
        expected_functional_fingerprint = str(
            dict(plan.get("candidate") or {})
            .get("functional_publication", {})
            .get("plan_fingerprint")
            or ""
        )
        if (
            str(functional_plan.get("plan_fingerprint") or "")
            != expected_functional_fingerprint
        ):
            raise ValueError(
                "functional publication differs from approved candidate"
            )
        functional_publication = functional._apply_plan_locked(  # noqa: SLF001
            functional_plan,
            confirm_fingerprint=str(functional_plan["plan_fingerprint"]),
            backup_dir=backup_root,
        )
        with _connect(runtime.db_path) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                    recovery_id TEXT PRIMARY KEY,
                    plan_fingerprint TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    changed_rows INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            recovery_id = (
                "s26r_"
                + str(plan["fingerprint"]).split(":", 1)[-1][:24]
            )
            conn.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}(
                    recovery_id,plan_fingerprint,applied_at,backup_path,
                    changed_rows,report_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    recovery_id,
                    plan["fingerprint"],
                    applied_at,
                    str(backup_path),
                    len(plan.get("changes") or []),
                    json.dumps(
                        dict(plan), ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            conn.commit()
        post = build_plan(runtime.db_path)
        if post["would_change"]:
            raise ValueError("post-apply readback is not idempotent")
        if post["non_target_before_digest"] != plan["non_target_before_digest"]:
            raise ValueError("non-target invariant changed after recovery")
        readback = dict(post["readback"])
        if (
            int(readback.get("active_count") or 0) != 1
            or int(readback.get("excluded_count") or 0) != 1
            or int(
                readback.get("active_financial_capital_event_count") or 0
            )
            <= 0
            or int(
                readback.get("archived_financial_capital_event_count") or 0
            )
            != 0
            or int(readback.get("ff_receipt_count") or 0) != 1
            or int(readback.get("ff_cost_layer_count") or 0) != 1
            or _money(readback.get("current_ff_cost_layer_capital_rub"))
            != EXPECTED_ACTIVE_FF_CAPITAL_RUB
        ):
            raise ValueError(
                "post-apply document/capital/FF chain invariant failed"
            )
    except Exception:
        restore_verified_supplier_backup(backup_path, runtime.db_path)
        raise
    return {
        **dict(plan),
        "mode": "apply",
        "applied": True,
        "backup": backup,
        "targeted_recalculation": queue,
        "capital_reconciliation": capital_reconciliation,
        "supplier_cost_rebuild": supplier_cost_rebuild,
        "functional_publication": functional_publication,
        "post_apply": {
            "idempotent": True,
            "changed_rows": int(plan["expected_affected_rows"]),
            "readback": readback,
            "non_target_digest": post["non_target_before_digest"],
        },
    }


def _shipment(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
        (SHIPMENT_ID,),
    ).fetchone()
    if row is None:
        raise ValueError(f"target shipment is missing: {SHIPMENT_ID}")
    return dict(row)


def _documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT document.*,
               COALESCE((
                   SELECT SUM(COALESCE(line.amount_rub,0))
                   FROM sheet_vitrina_v1_supplier_financial_expense_lines line
                   WHERE line.financial_document_id=document.document_id
               ),0) expense_amount_rub,
               COALESCE((
                   SELECT COUNT(*)
                   FROM sheet_vitrina_v1_supplier_financial_expense_lines line
                   WHERE line.financial_document_id=document.document_id
               ),0) expense_line_count
        FROM sheet_vitrina_v1_supplier_financial_documents document
        WHERE supplier_order_id=?
        ORDER BY document_id
        """,
        (SHIPMENT_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def _validate_identity(
    shipment: Mapping[str, Any], documents: Iterable[Mapping[str, Any]]
) -> None:
    if str(shipment.get("invoice_no") or "") != INVOICE_NO:
        raise ValueError("target shipment invoice identity mismatch")
    by_id = {str(item.get("document_id") or ""): item for item in documents}
    for document_id in TARGET_DOCUMENT_IDS:
        if document_id not in by_id:
            raise ValueError(f"target financial document is missing: {document_id}")
        document = by_id[document_id]
        if not _is_invoice_136(document):
            raise ValueError(f"financial document identity mismatch: {document_id}")
        if str(document.get("file_sha256") or "") != EXPECTED_FILE_SHA256:
            raise ValueError(f"financial document SHA mismatch: {document_id}")
        if _money(document.get("total_amount_rub")) != EXPECTED_AMOUNT_RUB:
            raise ValueError(f"financial document amount mismatch: {document_id}")
        if _money(document.get("expense_amount_rub")) != EXPECTED_AMOUNT_RUB:
            raise ValueError(
                f"financial expense-line conservation mismatch: {document_id}"
            )
        if int(document.get("expense_line_count") or 0) != 1:
            raise ValueError(
                f"financial document must have exactly one expense line: {document_id}"
            )


def _is_invoice_136(document: Mapping[str, Any]) -> bool:
    return (
        str(document.get("document_type") or "") == "logistics_invoice"
        and str(document.get("document_number") or "") == DOCUMENT_NUMBER
        and str(document.get("document_date") or "") == "2026-07-15"
        and str(document.get("currency") or "").upper() == "RUB"
        and _money(document.get("total_amount_rub")) == EXPECTED_AMOUNT_RUB
    )


def _readback_projection(
    shipment: Mapping[str, Any],
    documents: Iterable[Mapping[str, Any]],
    side_effects: Mapping[str, Any],
) -> dict[str, Any]:
    docs = [dict(item) for item in documents if _is_invoice_136(item)]
    return {
        "shipment_id": SHIPMENT_ID,
        "invoice_no": str(shipment.get("invoice_no") or ""),
        "actual_shipment_date": str(shipment.get("actual_shipment_date") or ""),
        "actual_ff_acceptance_date": str(
            shipment.get("actual_ff_acceptance_date") or ""
        ),
        "expenses_complete": bool(shipment.get("expenses_complete")),
        "invoice_136": [
            {
                "document_id": item["document_id"],
                "parse_status": item["parse_status"],
                "file_sha256": item["file_sha256"],
                "amount_rub": _money(item["total_amount_rub"]),
                "expense_line_count": int(item["expense_line_count"]),
                "expense_amount_rub": _money(item["expense_amount_rub"]),
            }
            for item in docs
        ],
        "active_count": sum(
            1 for item in docs if str(item["parse_status"]) != "excluded"
        ),
        "excluded_count": sum(
            1 for item in docs if str(item["parse_status"]) == "excluded"
        ),
        **dict(side_effects),
    }


def _side_effects(conn: sqlite3.Connection) -> dict[str, Any]:
    receipt_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_ff_stock_operations
            WHERE source_key=?
            """,
            (f"supplier_shipment_acceptance:{SHIPMENT_ID}",),
        ).fetchone()[0]
    )
    layer_rows = conn.execute(
        """
        SELECT layer_id,status,accepted_ff_date,reconciliation_status,
               reconciliation_delta_rub,is_current
        FROM sheet_vitrina_v1_supplier_ff_cost_layers
        WHERE supplier_shipment_id=? AND is_current=1
        ORDER BY layer_id
        """,
        (SHIPMENT_ID,),
    ).fetchall()
    current_layer_capital = conn.execute(
        """
        SELECT COALESCE(SUM(line.line_total_cost_rub),0)
        FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line
        JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer
          ON layer.layer_id=line.layer_id
        WHERE layer.supplier_shipment_id=? AND layer.is_current=1
        """,
        (SHIPMENT_ID,),
    ).fetchone()[0]
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    capital_counts = {
        ACTIVE_DOCUMENT_ID: 0,
        ARCHIVED_DOCUMENT_ID: 0,
    }
    if "sheet_vitrina_v1_own_capital_events" in tables:
        for document_id in capital_counts:
            capital_counts[document_id] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_own_capital_events
                    WHERE event_type='cost_payment'
                      AND event_id LIKE ? ESCAPE '\\'
                    """,
                    (
                        _literal_like_prefix(
                            f"cost_payment:financial_expense:{document_id}:"
                        ),
                    ),
                ).fetchone()[0]
            )
    return {
        "ff_receipt_count": receipt_count,
        "ff_cost_layer_count": len(layer_rows),
        "ff_cost_layers": [dict(row) for row in layer_rows],
        "current_ff_cost_layer_capital_rub": _money(current_layer_capital),
        "active_financial_capital_event_count": capital_counts[
            ACTIVE_DOCUMENT_ID
        ],
        "archived_financial_capital_event_count": capital_counts[
            ARCHIVED_DOCUMENT_ID
        ],
    }


def _target_digest(
    shipment: Mapping[str, Any], documents: Iterable[Mapping[str, Any]]
) -> str:
    return "sha256:" + _hash(
        {
            "shipment": {
                key: shipment.get(key)
                for key in (
                    "shipment_id",
                    "invoice_no",
                    "actual_shipment_date",
                    "actual_ff_acceptance_date",
                    "expenses_complete",
                    "updated_at",
                )
            },
            "documents": [
                {
                    key: item.get(key)
                    for key in (
                        "document_id",
                        "parse_status",
                        "file_sha256",
                        "total_amount_rub",
                        "expense_amount_rub",
                        "expense_line_count",
                        "updated_at",
                    )
                }
                for item in documents
                if str(item.get("document_id") or "") in TARGET_DOCUMENT_IDS
                or _is_invoice_136(item)
            ],
        }
    )


def _non_target_digest(conn: sqlite3.Connection) -> str:
    documents = [
        dict(row)
        for row in conn.execute(
            """
            SELECT document_id,supplier_order_id,parse_status,file_sha256,
                   total_amount_rub,updated_at
            FROM sheet_vitrina_v1_supplier_financial_documents
            WHERE document_id NOT IN (?,?)
              AND NOT (
                  supplier_order_id=? AND document_type='logistics_invoice'
                  AND document_number=? AND document_date='2026-07-15'
              )
            ORDER BY document_id
            """,
            (
                ACTIVE_DOCUMENT_ID,
                ARCHIVED_DOCUMENT_ID,
                SHIPMENT_ID,
                DOCUMENT_NUMBER,
            ),
        ).fetchall()
    ]
    shipments = [
        dict(row)
        for row in conn.execute(
            """
            SELECT shipment_id,updated_at,expenses_complete,actual_shipment_date,
                   actual_ff_acceptance_date
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id<>? ORDER BY shipment_id
            """,
            (SHIPMENT_ID,),
        ).fetchall()
    ]
    return "sha256:" + _hash({"documents": documents, "shipments": shipments})


def _supplier_functional_fingerprint_projection(
    conn: sqlite3.Connection,
    *,
    recovery_end_date: str,
) -> dict[str, Any]:
    sources = _functional_local_source_view(
        _source_rows(
            conn,
            recovery_end_date=recovery_end_date,
            include_historical_correction=True,
        )
    )
    allocation = _supplier_cost_allocations(sources).get(SHIPMENT_ID)
    if not allocation:
        raise ValueError(
            "current source has no canonical 26GN390 supplier allocation"
        )
    active_row = conn.execute(
        """
        SELECT active.version_id,state.source_fingerprint,
               state.calculation_fingerprint
        FROM sheet_vitrina_v1_warehouse_functional_active active
        LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_states state
          ON state.version_id=active.version_id AND state.shipment_id=?
        WHERE active.slot=1
        """,
        (SHIPMENT_ID,),
    ).fetchone()
    active = dict(active_row) if active_row is not None else {}
    current_source = str(allocation.get("source_fingerprint") or "")
    current_calculation = str(
        allocation.get("calculation_fingerprint") or ""
    )
    active_source = str(active.get("source_fingerprint") or "")
    active_calculation = str(active.get("calculation_fingerprint") or "")
    return {
        "active_version_id": str(active.get("version_id") or ""),
        "active_source_fingerprint": active_source,
        "active_calculation_fingerprint": active_calculation,
        "current_source_fingerprint": current_source,
        "current_calculation_fingerprint": current_calculation,
        "matches_active_version": bool(
            current_source
            and current_calculation
            and current_source == active_source
            and current_calculation == active_calculation
        ),
    }


def _stable_business_timestamp() -> str:
    """Return one deterministic publication timestamp for the local business day."""

    current = datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    return current.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()


def _candidate_recovery_projection(
    db_path: Path,
    *,
    planned_at: str,
    source_revision: str,
    reconcile_capital: bool,
    rebuild_supplier_costs: bool,
) -> dict[str, Any]:
    """Simulate the entire bounded mutation on a coherent disposable snapshot."""

    from decimal import Decimal

    with TemporaryDirectory(prefix="supplier-26gn390-candidate-") as temp_dir:
        candidate_runtime_dir = Path(temp_dir) / "runtime"
        candidate_runtime_dir.mkdir(parents=True, exist_ok=True)
        candidate_db = candidate_runtime_dir / "registry_upload_runtime.sqlite3"
        _readonly_sqlite_copy(db_path, candidate_db)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=candidate_runtime_dir)
        shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
        if not shipment:
            raise ValueError("candidate snapshot lost the target shipment")

        capital_reconciliation: dict[str, Any] | None = None
        if reconcile_capital:
            capital = OwnProductCapitalBlock(
                runtime=runtime,
                timestamp_factory=lambda: planned_at,
            )
            removed = capital.remove_financial_document_expenses(
                ARCHIVED_DOCUMENT_ID,
                recalculate=False,
            )
            materialized = capital.materialize_persisted_expense_events(
                shipment_id=SHIPMENT_ID,
            )
            capital_reconciliation = {
                "archived_events_removed": int(
                    removed.get("removed_event_count") or 0
                ),
                "active_chain": materialized,
            }

        nm_ids = sorted(
            {
                int(item.get("internal_nm_id") or 0)
                for item in shipment.get("lines") or []
                if int(item.get("internal_nm_id") or 0) > 0
            }
        )
        queue = enqueue_warehouse_targeted_recalculation(
            runtime=runtime,
            stable_source_id=f"supplier_shipment:{SHIPMENT_ID}",
            source_revision=source_revision,
            effective_date=str(
                (shipment.get("header") or {}).get("actual_shipment_date")
                or (shipment.get("header") or {}).get("invoice_date")
                or planned_at[:10]
            )[:10],
            affected_nm_ids=nm_ids,
            requested_at=planned_at,
        )
        supplier_cost_rebuild: dict[str, Any] | None = None
        if rebuild_supplier_costs:
            supplier_cost_rebuild = OurWbCostBlock(
                runtime=runtime,
                timestamp_factory=lambda: planned_at,
            ).materialize_supplier_ff_cost_layer(SHIPMENT_ID)

        with _connect(candidate_db) as conn:
            sources = _functional_local_source_view(
                _source_rows(
                    conn,
                    recovery_end_date=planned_at[:10],
                    include_historical_correction=True,
                )
            )
        allocation = _supplier_cost_allocations(sources).get(SHIPMENT_ID)
        if not allocation:
            raise ValueError("candidate has no canonical supplier allocation")
        archived_source_ids = {
            str(item.get("document_id") or "")
            for item in sources.get("financial_documents") or []
            if str(item.get("parse_status") or "") == "excluded"
        }
        active_invoice_136_ids = {
            str(item.get("document_id") or "")
            for item in sources.get("financial_documents") or []
            if _is_invoice_136(item)
        }
        if ARCHIVED_DOCUMENT_ID in archived_source_ids:
            raise ValueError("archived document leaked into active candidate source")
        if active_invoice_136_ids != {ACTIVE_DOCUMENT_ID}:
            raise ValueError(
                "candidate must contain exactly the active invoice-136 source"
            )

        controls = list(allocation.get("document_controls") or [])
        eligible_components = sum(
            int(item.get("eligible_component_count") or 0) for item in controls
        )
        allocated_components = sum(
            int(item.get("allocated_component_count") or 0) for item in controls
        )
        eligible_amount = sum(
            (
                Decimal(str(item.get("eligible_amount_rub") or "0"))
                for item in controls
            ),
            Decimal("0"),
        )
        allocated_amount = sum(
            (
                Decimal(str(item.get("allocated_amount_rub") or "0"))
                for item in controls
            ),
            Decimal("0"),
        )
        unallocated_amount = eligible_amount - allocated_amount
        if (
            eligible_components != 9
            or allocated_components != 9
            or _money(eligible_amount) != EXPECTED_ACTIVE_FF_CAPITAL_RUB
            or _money(allocated_amount) != EXPECTED_ACTIVE_FF_CAPITAL_RUB
            or _money(unallocated_amount) != "0.00"
            or _money(allocation.get("capital_rub"))
            != EXPECTED_ACTIVE_FF_CAPITAL_RUB
            or list(allocation.get("blockers") or [])
        ):
            raise ValueError(
                "candidate does not match approved 9/9 and 9,102,131.12 ₽ allocation"
            )

        functional = WarehouseFunctionalBlock(
            runtime=runtime,
            timestamp_factory=lambda: planned_at,
        )
        functional_plan = functional.build_emergency_rebuild_plan()
        diff_rows = list(
            dict(functional_plan.get("diff") or {}).get("lines") or []
        )
        if any(
            int(item.get("nm_id") or 0) not in set(nm_ids)
            or _money(item.get("quantity_delta")) != "0.00"
            for item in diff_rows
        ):
            raise ValueError(
                "candidate functional diff escapes target shipment SKUs or changes quantity"
            )
        supplier_state = next(
            (
                dict(item)
                for item in functional_plan.get("supplier_cost_states") or []
                if str(item.get("shipment_id") or "") == SHIPMENT_ID
            ),
            None,
        )
        if supplier_state is None:
            raise ValueError(
                "candidate functional version omitted target supplier cost state"
            )
        if (
            str(supplier_state.get("source_fingerprint") or "")
            != str(allocation.get("source_fingerprint") or "")
            or str(supplier_state.get("calculation_fingerprint") or "")
            != str(allocation.get("calculation_fingerprint") or "")
            or not bool(supplier_state.get("expenses_complete"))
            or not bool(supplier_state.get("calculation_available"))
        ):
            raise ValueError(
                "candidate functional version does not bind exact supplier fingerprints"
            )
        return {
            "capital_reconciliation": capital_reconciliation,
            "targeted_recalculation": {
                key: queue.get(key)
                for key in (
                    "queue_id",
                    "stable_source_id",
                    "source_revision",
                    "effective_date",
                    "affected_nm_ids",
                    "status",
                )
            },
            "supplier_cost_rebuild": (
                {
                    key: supplier_cost_rebuild.get(key)
                    for key in (
                        "supplier_shipment_id",
                        "layer_id",
                        "status",
                        "capital_rub",
                    )
                }
                if supplier_cost_rebuild
                else None
            ),
            "allocation": {
                "eligible_component_count": eligible_components,
                "allocated_component_count": allocated_components,
                "eligible_amount_rub": _money(eligible_amount),
                "allocated_amount_rub": _money(allocated_amount),
                "unallocated_amount_rub": _money(unallocated_amount),
                "capital_rub": _money(allocation.get("capital_rub")),
                "source_fingerprint": allocation.get("source_fingerprint"),
                "calculation_fingerprint": allocation.get(
                    "calculation_fingerprint"
                ),
                "active_invoice_136_document_ids": sorted(
                    active_invoice_136_ids
                ),
            },
            "functional_publication": {
                "plan_fingerprint": functional_plan.get("plan_fingerprint"),
                "base_active_version_id": functional_plan.get(
                    "base_active_version_id"
                ),
                "effective_date": functional_plan.get("effective_date"),
                "local_source_digest": functional_plan.get(
                    "local_source_digest"
                ),
                "calculation_digest": functional_plan.get(
                    "calculation_digest"
                ),
                "target_supplier_cost_state": supplier_state,
                "diff": functional_plan.get("diff"),
                "invariants": functional_plan.get("invariants"),
            },
        }


def _readonly_sqlite_copy(source: Path, target: Path) -> None:
    """Copy a live SQLite database through one query-only coherent snapshot."""

    if target.exists():
        raise ValueError(f"backup target already exists: {target}")
    admit_root_write(
        owner="supplier_26gn390_recovery",
        destination=target,
        predicted_output_bytes=predict_sqlite_backup_bytes(source),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as source_conn,
        sqlite3.connect(target) as target_conn,
    ):
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.backup(target_conn)
        target_conn.commit()
    with sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True) as check:
        check.execute("PRAGMA query_only=ON")
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    if target.stat().st_size <= 0 or integrity.lower() != "ok":
        target.unlink(missing_ok=True)
        raise ValueError("read-only candidate SQLite snapshot failed integrity_check")


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _money(value: Any) -> str:
    from decimal import Decimal, ROUND_HALF_UP

    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _literal_like_prefix(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        + "%"
    )


if __name__ == "__main__":
    raise SystemExit(main())
