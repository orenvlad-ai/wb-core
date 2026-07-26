"""End-to-end local smoke for one exact supplier/publication approval chain."""

from argparse import Namespace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_shipment_factual_date_correction_smoke import (
    DOCUMENT_ID,
    HISTORICAL_SHIPMENT_ID,
    SHIPMENT_ID,
    _materialize_legacy_conflict,
    _runtime_fixture,
)
from apps.supplier_shipment_publication_chain import (
    _verify_disposable_publication_no_op,
    apply_chain,
    build_chain_report,
)
from packages.application.canonical_cost_engine import CanonicalCostEngine
from packages.application.registry_upload_db_backed_runtime import _connect


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _runtime_fixture(root / "runtime")
        _materialize_legacy_conflict(runtime)
        CanonicalCostEngine(runtime=runtime).rebuild(
            date_from="2026-07-01", date_to="2026-07-15"
        )
        args = Namespace(
            runtime_dir=str(runtime.runtime_dir),
            shipment_id=SHIPMENT_ID,
            new_actual_shipment_date="2026-06-25",
            actor="chain-smoke",
            expected_old_value=None,
            expected_invoice_no="26GN390",
            expected_invoice_document_id=DOCUMENT_ID,
            historical_status_shipment_id=HISTORICAL_SHIPMENT_ID,
            historical_status_action="activate",
            historical_status_exception="legacy_ff_accepted_without_date",
            historical_expected_invoice_no="26GN237",
            historical_expected_invoice_date="2026-03-29",
            historical_expected_shipment_date="2026-05-22",
            historical_expected_current_exception=None,
            historical_expected_evidence_fingerprint=None,
            historical_reason="legacy_ff_accepted_without_known_factual_date",
            historical_provenance="operator_confirmed_historical_registry_state",
            historical_reverses_event_id=None,
            apply=False,
            fingerprint="",
            backup_dir=str(root / "supplier-backups"),
            prepare_backup_dir=None,
            publication_date_from="2026-07-01",
            publication_date_to="2026-07-14",
            publication_fingerprint="",
            publication_backup_dir=str(root / "publication-backups"),
            chain_fingerprint="",
        )
        first = build_chain_report(args)
        second = build_chain_report(args)
        assert first["chain_fingerprint"] == second["chain_fingerprint"]
        assert first["supplier_fingerprint"] == second["supplier_fingerprint"]
        assert first["publication_fingerprint"] == second["publication_fingerprint"]
        assert first["publication_second_run"] == second["publication_second_run"]
        assert first["publication_second_run_changed_cells"] == 0
        assert first["publication_second_run"]["idempotent"] is True
        assert first["publication_second_run"]["applied_to_production"] is False
        try:
            _verify_disposable_publication_no_op(
                runtime.db_path,
                publication={},
                date_from="2026-07-01",
                date_to="2026-07-14",
            )
        except ValueError as exc:
            assert "disposable supplier candidate" in str(exc)
        else:
            raise AssertionError("production-shaped publication no-op proof was accepted")
        args.apply = True
        args.fingerprint = first["supplier_fingerprint"]
        args.publication_fingerprint = first["publication_fingerprint"]
        args.chain_fingerprint = first["chain_fingerprint"]
        try:
            apply_chain(args)
        except ValueError as exc:
            assert "chain apply is disabled" in str(exc)
        else:
            raise AssertionError("legacy supplier/publication chain unexpectedly applied")
        with _connect(runtime.db_path) as conn:
            job_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM "
                    "sheet_vitrina_v1_supplier_publication_chain_jobs"
                ).fetchone()[0]
            )
        assert job_count == 0
        assert not Path(args.backup_dir).exists()
        assert not Path(args.publication_backup_dir).exists()
    print("supplier_shipment_publication_chain_smoke: dry-run only; legacy apply disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
