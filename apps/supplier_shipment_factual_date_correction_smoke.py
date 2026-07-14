"""Atomic, cross-cutover and idempotency smoke for supplier factual-date correction."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.canonical_cost_engine_backfill import run as run_canonical_backfill  # noqa: E402
from apps.canonical_cost_engine_smoke import (  # noqa: E402
    _insert_fallback_production,
    _insert_ff_balance,
    _insert_primary,
    _insert_snapshot,
    _insert_supplier_payment,
)
from packages.application.own_product_capital import OwnProductCapitalBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)


SHIPMENT_ID = "sup_b3070385b00b4eb680bd805d751d65be"
DOCUMENT_ID = "tdoc_baa149260aad400681f225761e0cbcc0"


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _runtime_fixture(root / "runtime")
        _materialize_legacy_conflict(runtime)
        inode = runtime.db_path.stat().st_ino
        before = _evidence(runtime)
        block = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-14T12:00:00Z",
        )
        dry = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
        )
        _assert(dry["scope"] == {"date_from": "2026-07-01", "date_to": "2026-07-14"}, "bounded scope")
        _assert(dry["crosses_cutover"], "legacy July evidence must prove cross-cutover")
        _assert(dry["derived_status"]["order_status"] == "in_transit", "derived in-transit")
        _assert(dry["preflight"]["partial_state_detected"] is False, "header still old before apply")
        _assert(dry["rebuild"]["second_run_changed"] == 0, "candidate second rebuild")
        _assert(dry["reconciliation"]["status"] == "ok", "candidate reconciliation")
        _assert(dry["baseline_fingerprint_before"] == dry["baseline_fingerprint_after"], "baseline fingerprint")
        _assert(dry["would_change"], "first dry-run changes target")
        later_dry = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-14T13:00:00Z",
        ).dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
        )
        _assert(
            later_dry["fingerprint"] == dry["fingerprint"],
            "exact apply fingerprint must stay stable across same-business-day dry-runs",
        )

        def fail_before_post_verify(phase: str) -> None:
            if phase == "before_post_verify":
                raise RuntimeError("synthetic post-commit failure")

        failing = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-14T12:00:00Z",
            failure_injector=fail_before_post_verify,
        )
        try:
            failing.apply(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-06-25",
                actor="smoke",
                fingerprint=dry["fingerprint"],
                backup_dir=root / "failed-backups",
                expected_invoice_no="26GN390",
                expected_invoice_document_id=DOCUMENT_ID,
            )
        except RuntimeError as exc:
            _assert("synthetic" in str(exc), "synthetic failure surfaced")
        else:
            raise AssertionError("forced post-commit verification failure unexpectedly succeeded")
        _assert(runtime.db_path.stat().st_ino == inode, "restore preserves live inode")
        _assert(_evidence(runtime) == before, "forced failure leaves no partial commit")

        applied = block.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            fingerprint=dry["fingerprint"],
            backup_dir=root / "backups",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
        )
        _assert(applied["applied"] is True, "correction applied")
        _assert(applied["post_run"]["changed"] == 0, "post apply rebuild zero")
        _assert(runtime.db_path.stat().st_ino == inode, "apply preserves live inode")
        after = _evidence(runtime)
        _assert(after["actual_shipment_date"] == "2026-06-25", "header corrected")
        _assert(after["order_status"] == "in_transit", "cache status corrected")
        _assert(after["legacy_effective_date"] == "2026-07-25", "legacy evidence preserved")
        _assert(after["other_shipment"] == before["other_shipment"], "other shipment preserved")
        _assert(after["invoice_amount_total"] == before["invoice_amount_total"], "invoice preserved")
        _assert(applied["backup"]["mode"] == "0600", "verified backup")
        repeated = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
        )
        _assert(repeated["would_change"] is False, "repeat dry-run is zero-change")
        repeated_apply = block.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            fingerprint=repeated["fingerprint"],
            backup_dir=root / "backups",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
        )
        _assert(repeated_apply["applied"] is False, "repeat apply does not write")
    _clearing_cases()
    print("supplier_shipment_factual_date_correction_smoke: ok")
    return 0


def _runtime_fixture(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        _insert_primary(conn)
        _insert_fallback_production(conn, nm_id=222, shipment_id=SHIPMENT_ID)
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_supplier_shipments
            SET actual_shipment_date='2026-07-25',order_status='in_transit',
                invoice_no='26GN390',invoice_document_id=?
            WHERE shipment_id=?
            """,
            (DOCUMENT_ID, SHIPMENT_ID),
        )
        _insert_supplier_payment(conn, shipment_id=SHIPMENT_ID, cny="1000", rub="10000")
        _insert_ff_balance(conn, nm_id=111, quantity=6750)
        _insert_snapshot(conn, "2026-05-16", {222: {"onec_FF_STOCK_unit_cost_rub": 80}})
        _insert_snapshot(conn, "2026-07-01", {111: {"stock_total": 93250}, 222: {"stock_total": 0}})
        conn.commit()
    dry = run_canonical_backfill(_backfill_args(runtime_dir))
    applied = run_canonical_backfill(
        _backfill_args(runtime_dir, apply=True, fingerprint=dry["fingerprint"])
    )
    _assert(applied["applied"], "canonical fixture materialized")
    return runtime


def _materialize_legacy_conflict(runtime: RegistryUploadDbBackedRuntime) -> None:
    capital = OwnProductCapitalBlock(
        runtime=runtime,
        timestamp_factory=lambda: "2026-07-14T12:00:00Z",
    )
    capital.record_supplier_payment(
        payment_id="legacy-payment",
        shipment_id=SHIPMENT_ID,
        effective_date="2026-06-24",
        invoice_total_cny="1000",
        paid_cny="1000",
        paid_rub="10000",
        product_lines=[{"line_id": f"line-{SHIPMENT_ID}", "nm_id": 222, "qty": 100, "amount": 1000}],
        actual_shipment_date="2026-07-25",
        recalculate=False,
    )
    capital.materialize_supplier_boundaries(
        shipment_id=SHIPMENT_ID,
        actual_shipment_date="2026-07-25",
        actual_ff_acceptance_date=None,
        expenses_complete=True,
        recalculate=False,
    )
    try:
        capital.materialize_supplier_boundaries(
            shipment_id=SHIPMENT_ID,
            actual_shipment_date="2026-06-25",
            actual_ff_acceptance_date=None,
            expenses_complete=True,
            recalculate=False,
        )
    except ValueError as exc:
        _assert("movement identity already exists with different factual evidence" in str(exc), "old conflict reproduced")
    else:
        raise AssertionError("legacy movement conflict was not reproduced")


def _evidence(runtime: RegistryUploadDbBackedRuntime) -> dict[str, object]:
    with _connect(runtime.db_path) as conn:
        header = conn.execute(
            "SELECT actual_shipment_date,order_status,invoice_amount_total FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (SHIPMENT_ID,),
        ).fetchone()
        legacy = conn.execute(
            "SELECT effective_date FROM sheet_vitrina_v1_own_capital_events WHERE event_id LIKE ? ORDER BY event_id LIMIT 1",
            (f"stage_transfer:supplier_dispatch:{SHIPMENT_ID}:%",),
        ).fetchone()
        other = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id='primary-june'"
        ).fetchone()
        canonical = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(physical_quantity+0),0) FROM sheet_vitrina_v1_canonical_cost_daily_state"
        ).fetchone()
    return {
        "actual_shipment_date": str(header["actual_shipment_date"] or ""),
        "order_status": str(header["order_status"] or ""),
        "invoice_amount_total": header["invoice_amount_total"],
        "legacy_effective_date": str(legacy["effective_date"] or "") if legacy else "",
        "other_shipment": tuple(other) if other else (),
        "canonical": tuple(canonical),
    }


def _backfill_args(runtime_dir: Path, *, apply: bool = False, fingerprint: str = "") -> Namespace:
    return Namespace(
        runtime_dir=str(runtime_dir),
        date_from="2026-07-01",
        date_to="2026-07-14",
        apply=apply,
        fingerprint=fingerprint,
        backup_dir=str(runtime_dir.parent / "canonical-backups"),
    )


def _clearing_cases() -> None:
    for with_acceptance, expected_status in ((False, "production"), (True, "accepted_ff")):
        with TemporaryDirectory() as tmp:
            runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
            runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
            shipment_id = "clear-with-acceptance" if with_acceptance else "clear-without-acceptance"
            with _connect(runtime.db_path) as conn:
                _ensure_schema(conn)
                _insert_fallback_production(conn, nm_id=333, shipment_id=shipment_id)
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_shipments
                    SET actual_shipment_date='2026-07-10',actual_ff_acceptance_date=?,order_status=?
                    WHERE shipment_id=?
                    """,
                    ("2026-07-12" if with_acceptance else None, expected_status, shipment_id),
                )
                conn.commit()
            block = SupplierShipmentFactualCorrectionBlock(
                runtime=runtime,
                timestamp_factory=lambda: "2026-07-14T12:00:00Z",
            )
            dry = block.dry_run(
                shipment_id=shipment_id,
                new_actual_shipment_date="",
                actor="smoke",
            )
            _assert(dry["derived_status"]["order_status"] == expected_status, "clearing derived status")
            applied = block.apply(
                shipment_id=shipment_id,
                new_actual_shipment_date="",
                actor="smoke",
                fingerprint=dry["fingerprint"],
                backup_dir=Path(tmp) / "backups",
            )
            _assert(applied["applied"], "clearing correction applied")
            detail = runtime.load_supplier_shipment(shipment_id) or {}
            header = detail.get("header") or {}
            _assert(not header.get("actual_shipment_date"), "shipment date cleared")
            _assert(header.get("order_status") == expected_status, "clearing status readback")
            if with_acceptance:
                _assert(header.get("actual_ff_acceptance_date") == "2026-07-12", "acceptance evidence preserved")


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
