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
import packages.application.supplier_shipment_factual_correction as correction_module  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)
from packages.application.supplier_shipment_status import (  # noqa: E402
    HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
)


SHIPMENT_ID = "sup_b3070385b00b4eb680bd805d751d65be"
DOCUMENT_ID = "tdoc_baa149260aad400681f225761e0cbcc0"
FACTUAL_SOURCE_SHA = "59910f328db9e0e47ab06839eae9d378e6abf49822566581fd85320ece03d9d4"
HISTORICAL_SHIPMENT_ID = "sup_b8009d513e12422cacb91e40983c16af"
HISTORICAL_DOCUMENT_ID = "tdoc_42087454b84d4977a48f987658c6becd"
HISTORICAL_SOURCE_SHA = "92e5a2d63a1330f6c4a7812d9c90425cf7707545a8ac318618449f17d6578085"


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
        historical_change = {
            "shipment_id": HISTORICAL_SHIPMENT_ID,
            "action": "activate",
            "exception_code": HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
            "expected_invoice_no": "26GN237",
            "expected_invoice_date": "2026-03-29",
            "expected_shipment_date": "2026-05-22",
            "reason": "legacy_ff_accepted_without_known_factual_date",
            "provenance": "operator_confirmed_historical_registry_state",
        }
        dry = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        _assert(dry["scope"] == {"date_from": "2026-07-01", "date_to": "2026-07-14"}, "bounded scope")
        _assert(dry["crosses_cutover"], "legacy July evidence must prove cross-cutover")
        _assert(dry["derived_status"]["order_status"] == "in_transit", "derived in-transit")
        _assert(dry["preflight"]["partial_state_detected"] is True, "legacy partial state detected")
        _assert(dry["source"] == "historical_factual_date_adoption", "truthful adoption source")
        _assert(dry["factual_date_already_correct_before_apply"], "date is already correct")
        _assert(dry["rebuild"]["second_run_changed"] == 0, "candidate second rebuild")
        _assert(dry["reconciliation"]["status"] == "ok", "candidate reconciliation")
        _assert(dry["baseline_fingerprint_before"] == dry["baseline_fingerprint_after"], "baseline fingerprint")
        _assert(dry["would_change"], "first dry-run changes target")
        _assert(
            dry["historical_status_change"]["derived_status"]["status_display"]
            == "Принято на ФФ · дата неизвестна",
            "historical display",
        )
        _assert(
            dry["historical_status_change"]["evidence_summary"][
                "existing_acceptance_operation_count"
            ]
            == 0,
            "historical signal has no acceptance movement",
        )
        first_collateral_digest = dry["collateral_invariant"]["source_digest"]
        with _connect(runtime.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_ready_snapshots
                SET refreshed_at='2026-07-14T12:30:00Z'
                WHERE as_of_date='2026-07-01'
                """
            )
            conn.commit()
        later_dry = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-14T13:00:00Z",
        ).dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        _assert(
            later_dry["fingerprint"] == dry["fingerprint"],
            "unrelated ready-snapshot activity must not stale the semantic fingerprint",
        )
        _assert(
            later_dry["collateral_invariant"]["source_digest"]
            != first_collateral_digest,
            "diagnostic collateral snapshot must still observe unrelated activity",
        )
        _assert(
            later_dry["dependency_closure_digest"]
            == dry["dependency_closure_digest"],
            "unrelated activity must preserve the bounded dependency closure",
        )
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_amount_total=COALESCE(invoice_amount_total,0)+1 WHERE shipment_id=?",
                (SHIPMENT_ID,),
            )
            conn.commit()
        try:
            block.apply(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-06-25",
                actor="smoke",
                fingerprint=dry["fingerprint"],
                backup_dir=root / "target-drift-backups",
                expected_invoice_no="26GN390",
                expected_invoice_document_id=DOCUMENT_ID,
                historical_status_change=historical_change,
            )
        except ValueError as exc:
            _assert("exact current dry-run fingerprint" in str(exc), "target drift fail closed")
        else:
            raise AssertionError("target header drift unexpectedly passed")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_amount_total=invoice_amount_total-1 WHERE shipment_id=?",
                (SHIPMENT_ID,),
            )
            conn.commit()

        original_replace = correction_module._replace_canonical_tables

        def replace_with_collateral_write(conn, materialized):
            original_replace(conn, materialized)
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2099-01-01T00:00:00Z' WHERE as_of_date='2026-07-01'"
            )

        correction_module._replace_canonical_tables = replace_with_collateral_write
        try:
            block.apply(
                shipment_id=SHIPMENT_ID,
                new_actual_shipment_date="2026-06-25",
                actor="smoke",
                fingerprint=dry["fingerprint"],
                backup_dir=root / "collateral-backups",
                expected_invoice_no="26GN390",
                expected_invoice_document_id=DOCUMENT_ID,
                historical_status_change=historical_change,
            )
        except ValueError as exc:
            _assert("collateral rows" in str(exc), "collateral mutation fail closed")
        else:
            raise AssertionError("collateral mutation unexpectedly committed")
        finally:
            correction_module._replace_canonical_tables = original_replace
        with _connect(runtime.db_path) as conn:
            refreshed_at = conn.execute(
                "SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-07-01'"
            ).fetchone()[0]
        _assert(refreshed_at == "2026-07-14T12:30:00Z", "collateral mutation rolled back")

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
                historical_status_change=historical_change,
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
            historical_status_change=historical_change,
        )
        _assert(applied["applied"] is True, "correction applied")
        _assert(applied["post_run"]["changed"] == 0, "post apply rebuild zero")
        _assert(runtime.db_path.stat().st_ino == inode, "apply preserves live inode")
        after = _evidence(runtime)
        _assert(after["actual_shipment_date"] == "2026-06-25", "correct header date preserved")
        _assert(after["order_status"] == "in_transit", "cache status corrected")
        _assert(after["correction_source"] == "historical_factual_date_adoption", "truthful adoption persisted")
        _assert(after["correction_old_value"] == "2026-06-25", "adoption old value is truthful")
        _assert(after["correction_new_value"] == "2026-06-25", "adoption new value is truthful")
        _assert(after["legacy_effective_date"] == "2026-07-25", "legacy evidence preserved")
        _assert(after["other_shipment"] == before["other_shipment"], "other shipment preserved")
        _assert(after["invoice_amount_total"] == before["invoice_amount_total"], "invoice preserved")
        _assert(after["historical_exception"] == HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE, "historical signal persisted")
        _assert(after["historical_ff_acceptance_date"] == "", "historical acceptance date remains null")
        _assert(after["historical_status"] == "accepted_ff", "historical status cache")
        _assert(after["historical_acceptance_operations"] == before["historical_acceptance_operations"], "no acceptance movement")
        _assert(after["historical_ff_layers"] == before["historical_ff_layers"], "no FF cost layer")
        _assert(after["historical_event_count"] == 1, "one audited historical event")
        _assert(applied["backup"]["mode"] == "0600", "verified backup")
        repeated_change = {
            **historical_change,
            "expected_current_exception": HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
        }
        repeated = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=repeated_change,
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
            historical_status_change=repeated_change,
        )
        _assert(repeated_apply["applied"] is False, "repeat apply does not write")
        revert_change = {
            **historical_change,
            "action": "revert",
            "expected_current_exception": HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
            "reverses_event_id": "sshse_" + dry["fingerprint"][:24],
        }
        revert = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=revert_change,
        )
        reverted = block.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="smoke",
            fingerprint=revert["fingerprint"],
            backup_dir=root / "backups",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=revert_change,
        )
        _assert(reverted["applied"], "controlled historical revert applied")
        reverted_evidence = _evidence(runtime)
        _assert(reverted_evidence["historical_exception"] == "", "historical exception reverted")
        _assert(reverted_evidence["historical_status"] == "production", "revert restores derived production status")
        _assert(reverted_evidence["historical_acceptance_operations"] == before["historical_acceptance_operations"], "revert creates no movement")
        _assert(reverted_evidence["historical_ff_layers"] == before["historical_ff_layers"], "revert creates no FF layer")
        _assert(reverted_evidence["historical_event_count"] == 2, "reversal audit recorded")
    _clearing_cases()
    _canonical_rollforward_case()
    print("supplier_shipment_factual_date_correction_smoke: ok")
    return 0


def _runtime_fixture(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        _insert_primary(conn)
        _insert_fallback_production(conn, nm_id=222, shipment_id=SHIPMENT_ID)
        _insert_fallback_production(
            conn, nm_id=333, shipment_id=HISTORICAL_SHIPMENT_ID
        )
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_supplier_shipments
            SET actual_shipment_date='2026-06-25',order_status='in_transit',
                invoice_no='26GN390',invoice_document_id=?,source_file_sha256=?
            WHERE shipment_id=?
            """,
            (DOCUMENT_ID, FACTUAL_SOURCE_SHA, SHIPMENT_ID),
        )
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_supplier_shipments
            SET actual_shipment_date=NULL,actual_ff_acceptance_date=NULL,
                order_status='accepted_ff',invoice_no='26GN237',
                invoice_date='2026-03-29',shipment_date='2026-05-22',
                invoice_document_id=?,source_file_sha256=?
            WHERE shipment_id=?
            """,
            (HISTORICAL_DOCUMENT_ID, HISTORICAL_SOURCE_SHA, HISTORICAL_SHIPMENT_ID),
        )
        _insert_supplier_payment(conn, shipment_id=SHIPMENT_ID, cny="1000", rub="10000")
        _insert_ff_balance(conn, nm_id=111, quantity=6750)
        _insert_snapshot(
            conn,
            "2026-05-16",
            {
                222: {"onec_FF_STOCK_unit_cost_rub": 80},
                333: {"onec_FF_STOCK_unit_cost_rub": 90},
            },
        )
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
        historical = conn.execute(
            """
            SELECT actual_ff_acceptance_date,historical_status_exception,order_status
            FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?
            """,
            (HISTORICAL_SHIPMENT_ID,),
        ).fetchone()
        historical_acceptance_operations = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_object_id=?",
            (HISTORICAL_SHIPMENT_ID,),
        ).fetchone()[0]
        historical_ff_layers = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE supplier_shipment_id=?",
            (HISTORICAL_SHIPMENT_ID,),
        ).fetchone()[0]
        historical_event_count = (
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_shipment_historical_status_events WHERE shipment_id=?",
                (HISTORICAL_SHIPMENT_ID,),
            ).fetchone()[0]
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_supplier_shipment_historical_status_events'"
            ).fetchone()
            else 0
        )
        correction = (
            conn.execute(
                """
                SELECT source,old_value,new_value
                FROM sheet_vitrina_v1_supplier_shipment_factual_corrections
                WHERE shipment_id=? AND status='success'
                ORDER BY completed_at DESC,correction_id DESC LIMIT 1
                """,
                (SHIPMENT_ID,),
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_supplier_shipment_factual_corrections'"
            ).fetchone()
            else None
        )
    return {
        "actual_shipment_date": str(header["actual_shipment_date"] or ""),
        "order_status": str(header["order_status"] or ""),
        "invoice_amount_total": header["invoice_amount_total"],
        "legacy_effective_date": str(legacy["effective_date"] or "") if legacy else "",
        "other_shipment": tuple(other) if other else (),
        "canonical": tuple(canonical),
        "historical_ff_acceptance_date": str(historical["actual_ff_acceptance_date"] or ""),
        "historical_exception": str(historical["historical_status_exception"] or ""),
        "historical_status": str(historical["order_status"] or ""),
        "historical_acceptance_operations": int(historical_acceptance_operations),
        "historical_ff_layers": int(historical_ff_layers),
        "historical_event_count": int(historical_event_count),
        "correction_source": str(correction["source"] or "") if correction else "",
        "correction_old_value": str(correction["old_value"] or "") if correction else "",
        "correction_new_value": str(correction["new_value"] or "") if correction else "",
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


def _canonical_rollforward_case() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _runtime_fixture(root / "runtime")
        _materialize_legacy_conflict(runtime)
        block = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-15T12:00:00Z",
        )
        historical_change = {
            "shipment_id": HISTORICAL_SHIPMENT_ID,
            "action": "activate",
            "exception_code": HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
            "expected_invoice_no": "26GN237",
            "expected_invoice_date": "2026-03-29",
            "expected_shipment_date": "2026-05-22",
            "reason": "legacy_ff_accepted_without_known_factual_date",
            "provenance": "operator_confirmed_historical_registry_state",
        }
        dry = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="rollforward-smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        rollforward = dry["expected_canonical_rollforward"]
        _assert(rollforward["change_count"] > 0, "global daily rollforward is explicit")
        _assert(
            all(
                str(item["identity"]["as_of_date"]) == "2026-07-15"
                for item in rollforward["changes"]
            ),
            "rollforward rows are bounded to the current business date",
        )
        _assert(
            dry["collateral_invariant"]["candidate_source_unchanged"],
            "global canonical rollforward preserves source collateral",
        )
        applied = block.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="rollforward-smoke",
            fingerprint=dry["fingerprint"],
            backup_dir=root / "backups",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        _assert(applied["applied"], "exact canonical rollforward applied")
        _assert(applied["post_run"]["changed"] == 0, "rollforward second rebuild")
        repeated = block.dry_run(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="rollforward-smoke",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        _assert(
            repeated["expected_canonical_rollforward"]["change_count"] == 0,
            "second canonical rollforward is zero-change",
        )
        second_apply = block.apply(
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="rollforward-smoke",
            fingerprint=repeated["fingerprint"],
            backup_dir=root / "second-backups",
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_change=historical_change,
        )
        _assert(not second_apply["applied"], "second apply is a no-op")
        _assert(second_apply["post_run"]["changed"] == 0, "second apply changed zero rows")
def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
