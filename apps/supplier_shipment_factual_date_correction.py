#!/usr/bin/env python3
"""Guarded dry-run/apply runner for one supplier shipment factual-date correction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--shipment-id", required=True)
    parser.add_argument("--new-actual-shipment-date", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-old-value")
    parser.add_argument("--expected-invoice-no")
    parser.add_argument("--expected-invoice-document-id")
    parser.add_argument("--historical-status-shipment-id")
    parser.add_argument(
        "--historical-status-action", choices=("activate", "revert"), default="activate"
    )
    parser.add_argument("--historical-status-exception", default="legacy_ff_accepted_without_date")
    parser.add_argument("--historical-expected-invoice-no")
    parser.add_argument("--historical-expected-invoice-date")
    parser.add_argument("--historical-expected-shipment-date")
    parser.add_argument("--historical-expected-current-exception")
    parser.add_argument("--historical-expected-evidence-fingerprint")
    parser.add_argument("--historical-reason")
    parser.add_argument("--historical-provenance")
    parser.add_argument("--historical-reverses-event-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir")
    parser.add_argument("--prepare-backup-dir")
    return parser


def build_correction_request(args: argparse.Namespace) -> dict[str, object]:
    historical_status_change = None
    if str(args.historical_status_shipment_id or "").strip():
        historical_status_change = {
            "shipment_id": args.historical_status_shipment_id,
            "action": args.historical_status_action,
            "exception_code": args.historical_status_exception,
            "expected_invoice_no": args.historical_expected_invoice_no,
            "expected_invoice_date": args.historical_expected_invoice_date,
            "expected_shipment_date": args.historical_expected_shipment_date,
            "reason": args.historical_reason,
            "provenance": args.historical_provenance,
            "reverses_event_id": args.historical_reverses_event_id,
        }
        if args.historical_expected_current_exception is not None:
            historical_status_change["expected_current_exception"] = (
                args.historical_expected_current_exception
            )
        if args.historical_expected_evidence_fingerprint:
            historical_status_change["expected_evidence_fingerprint"] = (
                args.historical_expected_evidence_fingerprint
            )
    return {
        "shipment_id": args.shipment_id,
        "new_actual_shipment_date": args.new_actual_shipment_date,
        "actor": args.actor,
        "expected_old_value": args.expected_old_value,
        "expected_invoice_no": args.expected_invoice_no,
        "expected_invoice_document_id": args.expected_invoice_document_id,
        "require_cross_cutover_rebuild": True,
        "historical_status_change": historical_status_change,
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    block = SupplierShipmentFactualCorrectionBlock(runtime=runtime)
    common = build_correction_request(args)
    if not args.apply:
        result = block.dry_run(**common)
        if str(args.prepare_backup_dir or "").strip():
            result["prepared_backup"] = block.prepare_backup(Path(args.prepare_backup_dir))
        return result
    if not str(args.fingerprint or "").strip():
        raise ValueError("--apply requires --fingerprint from the exact current dry-run")
    if not str(args.backup_dir or "").strip():
        raise ValueError("--apply requires an explicit --backup-dir")
    return block.apply(
        **common,
        fingerprint=args.fingerprint,
        backup_dir=Path(args.backup_dir),
    )


def main() -> int:
    try:
        payload = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
