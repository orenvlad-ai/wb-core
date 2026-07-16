#!/usr/bin/env python3
"""Plan or apply the exact supplier-reconciliation -> vitrina-publication chain."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.canonical_cost_engine_vitrina_publication import (  # noqa: E402
    apply_publication,
    build_publication_report,
)
from apps.supplier_shipment_factual_date_correction import (  # noqa: E402
    build_correction_request,
    build_parser as build_supplier_parser,
)
from packages.application.canonical_cost_engine import CUTOVER_DATE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
    restore_verified_supplier_backup,
)


CHAIN_TABLE = "sheet_vitrina_v1_supplier_publication_chain_jobs"


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = build_supplier_parser()
    parser.description = __doc__
    parser.add_argument("--publication-date-from", default=CUTOVER_DATE)
    parser.add_argument("--publication-date-to", default=date.today().isoformat())
    parser.add_argument("--publication-fingerprint", default="")
    parser.add_argument("--publication-backup-dir", default="")
    parser.add_argument("--chain-fingerprint", default="")
    return parser


def _without_plans(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "plans"}


def _verify_disposable_publication_no_op(
    db_path: Path,
    *,
    publication: Mapping[str, Any],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Apply a publication plan only to the correction's disposable candidate."""

    resolved = db_path.resolve()
    if not (
        resolved.parent.name == "runtime"
        and resolved.parent.parent.name.startswith("supplier-factual-date-candidate-")
    ):
        raise ValueError("publication no-op proof requires a disposable supplier candidate")
    current = build_publication_report(
        resolved,
        date_from=date_from,
        date_to=date_to,
    )
    if current["fingerprint"] != publication["fingerprint"]:
        raise ValueError("disposable publication candidate drift")
    with _connect(resolved) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for day, plan_json in publication["plans"].items():
                updated = conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots "
                    "SET plan_json=? WHERE as_of_date=?",
                    (plan_json, day),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        f"disposable publication snapshot identity drift: {day}"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    no_op = build_publication_report(
        resolved,
        date_from=date_from,
        date_to=date_to,
    )
    if int(no_op["changed_cells"]) != 0:
        raise ValueError("disposable publication zero-change proof failed")
    if no_op["published_output_digest"] != publication["published_output_digest"]:
        raise ValueError("disposable publication output digest drift")
    return {
        **_without_plans(no_op),
        "mode": "disposable-candidate-proof",
        "applied_to_production": False,
        "idempotent": True,
    }


def build_chain_report(args: argparse.Namespace) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    block = SupplierShipmentFactualCorrectionBlock(runtime=runtime)
    correction_request = build_correction_request(args)
    with block.candidate(**correction_request) as candidate:
        supplier = dict(candidate["report"])
        publication = build_publication_report(
            Path(candidate["db_path"]),
            date_from=args.publication_date_from,
            date_to=args.publication_date_to,
        )
        publication_no_op = _verify_disposable_publication_no_op(
            Path(candidate["db_path"]),
            publication=publication,
            date_from=args.publication_date_from,
            date_to=args.publication_date_to,
        )
    approval = {
        "contract_name": "supplier_reconciliation_vitrina_publication_chain_v2",
        "operation_order": [
            "supplier_relevant_source_recheck",
            "fresh_supplier_backup",
            "exact_financial_document_confirmation",
            "supplier_reconciliation_apply",
            "supplier_post_apply_zero_change_rebuild",
            "fresh_publication_backup",
            "canonical_vitrina_publication_apply",
            "publication_zero_change_recheck",
            "api_and_ui_read_only_verification",
        ],
        "supplier_fingerprint": supplier["fingerprint"],
        "supplier_request_fingerprint": supplier["request_fingerprint"],
        "supplier_dependency_closure_digest": supplier[
            "dependency_closure_digest"
        ],
        "supplier_target_after_digest": supplier["target_after_digest"],
        "supplier_candidate_canonical_digest": supplier[
            "candidate_canonical_digest"
        ],
        "supplier_canonical_rollforward_fingerprint": supplier[
            "expected_canonical_rollforward"
        ]["fingerprint"],
        "supplier_canonical_rollforward_before_digest": supplier[
            "expected_canonical_rollforward"
        ]["before_digest"],
        "supplier_canonical_rollforward_after_digest": supplier[
            "expected_canonical_rollforward"
        ]["after_digest"],
        "supplier_canonical_rollforward_change_count": supplier[
            "expected_canonical_rollforward"
        ]["change_count"],
        "supplier_financial_document_confirmation": supplier.get(
            "financial_document_confirmation"
        ),
        "supplier_financial_document_canonical_before": supplier.get(
            "financial_document_canonical_before"
        ),
        "supplier_financial_document_canonical_after": supplier.get(
            "financial_document_canonical_after"
        ),
        "supplier_financial_document_evidence_fingerprint": str(
            (supplier.get("financial_document_confirmation") or {}).get(
                "evidence_fingerprint"
            )
            or ""
        ),
        "publication_fingerprint": publication["fingerprint"],
        "publication_snapshot_input_digest": publication[
            "snapshot_input_digest"
        ],
        "publication_canonical_input_digest": publication[
            "canonical_input_digest"
        ],
        "publication_output_digest": publication["published_output_digest"],
        "publication_changed_cells": publication["changed_cells"],
        "publication_second_run_fingerprint": publication_no_op["fingerprint"],
        "publication_second_run_changed_cells": publication_no_op["changed_cells"],
        "publication_second_run_output_digest": publication_no_op[
            "published_output_digest"
        ],
        "stop_conditions": [
            "supplier semantic fingerprint drift",
            "publication fingerprint drift",
            "transaction collateral before/after mismatch",
            "backup or integrity verification failure",
            "non-zero supplier second rebuild",
            "non-zero publication second dry-run",
        ],
    }
    return {
        **approval,
        "chain_fingerprint": _hash(approval),
        "mode": "dry-run",
        "applied": False,
        "supplier": supplier,
        "publication": _without_plans(publication),
        "publication_second_run": publication_no_op,
    }


def _ensure_chain_schema(conn: Any) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CHAIN_TABLE}(
            chain_job_id TEXT PRIMARY KEY,
            chain_fingerprint TEXT NOT NULL,
            supplier_fingerprint TEXT NOT NULL,
            publication_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            report_json TEXT NOT NULL DEFAULT '{{}}',
            error_message TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _save_job(
    db_path: Path,
    *,
    job_id: str,
    report: Mapping[str, Any],
    actor: str,
    status: str,
    phase: str,
    error: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(db_path) as conn:
        _ensure_chain_schema(conn)
        conn.execute(
            f"""
            INSERT INTO {CHAIN_TABLE}(
                chain_job_id,chain_fingerprint,supplier_fingerprint,
                publication_fingerprint,status,phase,actor,started_at,
                updated_at,completed_at,report_json,error_message
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chain_job_id) DO UPDATE SET
                status=excluded.status,phase=excluded.phase,
                updated_at=excluded.updated_at,completed_at=excluded.completed_at,
                report_json=excluded.report_json,error_message=excluded.error_message
            """,
            (
                job_id,
                report["chain_fingerprint"],
                report["supplier_fingerprint"],
                report["publication_fingerprint"],
                status,
                phase,
                actor,
                now,
                now,
                now if status in {"success", "error"} else None,
                json.dumps(report, ensure_ascii=False, sort_keys=True, default=str),
                error,
            ),
        )
        conn.commit()


def apply_chain(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_chain_report(args)
    required = {
        "chain": (args.chain_fingerprint, plan["chain_fingerprint"]),
        "supplier": (args.fingerprint, plan["supplier_fingerprint"]),
        "publication": (
            args.publication_fingerprint,
            plan["publication_fingerprint"],
        ),
    }
    for label, (provided, expected) in required.items():
        if str(provided or "") != str(expected):
            raise ValueError(f"exact {label} fingerprint mismatch")
    if not str(args.backup_dir or "").strip():
        raise ValueError("chain apply requires --backup-dir")
    if not str(args.publication_backup_dir or "").strip():
        raise ValueError("chain apply requires --publication-backup-dir")

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    block = SupplierShipmentFactualCorrectionBlock(runtime=runtime)
    correction_request = build_correction_request(args)
    job_id = "sspc_" + uuid4().hex
    _save_job(
        runtime.db_path,
        job_id=job_id,
        report=plan,
        actor=args.actor,
        status="running",
        phase="supplier_apply",
    )
    supplier_result: dict[str, Any] | None = None
    try:
        supplier_result = block.apply(
            **correction_request,
            fingerprint=plan["supplier_fingerprint"],
            backup_dir=Path(args.backup_dir),
        )
        live_publication = build_publication_report(
            runtime.db_path,
            date_from=args.publication_date_from,
            date_to=args.publication_date_to,
        )
        if live_publication["fingerprint"] != plan["publication_fingerprint"]:
            raise ValueError("post-supplier publication plan drift")
        _save_job(
            runtime.db_path,
            job_id=job_id,
            report=plan,
            actor=args.actor,
            status="running",
            phase="publication_apply",
        )
        publication_result = apply_publication(
            runtime.db_path,
            date_from=args.publication_date_from,
            date_to=args.publication_date_to,
            fingerprint=plan["publication_fingerprint"],
            backup_dir=Path(args.publication_backup_dir),
        )
        result = {
            **plan,
            "mode": "apply",
            "applied": True,
            "chain_job_id": job_id,
            "supplier_apply": supplier_result,
            "publication_apply": publication_result,
        }
        _save_job(
            runtime.db_path,
            job_id=job_id,
            report=result,
            actor=args.actor,
            status="success",
            phase="completed",
        )
        return result
    except Exception as exc:
        backup_path = str(((supplier_result or {}).get("backup") or {}).get("path") or "")
        restore = None
        if backup_path:
            restore = restore_verified_supplier_backup(
                Path(backup_path), runtime.db_path
            )
        failure = {**plan, "restore": restore, "error": str(exc)}
        _save_job(
            runtime.db_path,
            job_id=job_id,
            report=failure,
            actor=args.actor,
            status="error",
            phase="restored" if restore else "failed_before_supplier_commit",
            error=str(exc),
        )
        raise


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = apply_chain(args) if args.apply else build_chain_report(args)
    except Exception as exc:
        error = {"status": "error", "error": str(exc)}
        if isinstance(getattr(exc, "report", None), dict):
            error["collateral_diagnostic"] = exc.report
        print(
            json.dumps(
                error,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
