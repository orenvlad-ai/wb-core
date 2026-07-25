#!/usr/bin/env python3
"""Fixture proof for the reversible bank-statement source migration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_financial_source_migration import run  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    SupplierFinancialDocumentsBlock,
)


STAMP = "2026-07-26T09:00:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="supplier-source-migration-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        body = b"%PDF-1.4\nsame VTB statement source\n"
        source_sha256 = hashlib.sha256(body).hexdigest()
        old_paths: dict[str, str] = {}
        for index in (1, 2):
            shipment_id = f"source-migration-{index}"
            document_id = f"fdoc_source_migration_{index}"
            runtime.save_supplier_shipment(
                header={
                    "shipment_id": shipment_id,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "shipment_date": "2026-07-26",
                    "invoice_no": f"MIGRATION-{index}",
                    "currency": "CNY",
                },
                lines=[],
            )
            source = (
                runtime_dir
                / "supplier_financial_documents"
                / "files"
                / shipment_id
                / document_id
                / "statement.pdf"
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(body)
            old_paths[document_id] = str(source.relative_to(runtime_dir))
            runtime.save_supplier_financial_document(
                document={
                    "document_id": document_id,
                    "supplier_order_id": shipment_id,
                    "document_type": "bank_fee_statement",
                    "original_filename": "statement.pdf",
                    "stored_file_path": old_paths[document_id],
                    "file_content_type": "application/pdf",
                    "file_sha256": source_sha256,
                    "uploaded_at": STAMP,
                    "updated_at": STAMP,
                    "parse_status": "confirmed",
                    "normalized_parse": {
                        "document_type": "bank_fee_statement",
                        "account_currency": "RUB",
                        "operations": [],
                        "fee_rows": [],
                    },
                    "raw_parse": {},
                    "parser_version": "fixture-v1",
                },
                expense_lines=[],
            )
        orphan = (
            runtime_dir
            / "supplier_financial_documents"
            / "files"
            / "orphan-shipment"
            / "orphan-document"
            / "statement.pdf"
        )
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(body)
        old_epoch = time.time() - 2 * 24 * 60 * 60
        os.utime(orphan, (old_epoch, old_epoch))

        planned = run(action="dry-run", runtime_dir=runtime_dir)
        if len(planned.get("groups") or []) != 1:
            raise AssertionError(f"migration plan did not dedupe the SHA: {planned}")
        with patch.object(
            RegistryUploadDbBackedRuntime,
            "migrate_supplier_financial_source_paths",
            side_effect=RuntimeError("injected pre-database interruption"),
        ):
            try:
                run(action="apply", runtime_dir=runtime_dir)
            except RuntimeError as exc:
                if "injected pre-database interruption" not in str(exc):
                    raise
            else:
                raise AssertionError("migration interruption was not raised")
        prepared_manifest = json.loads(
            (
                runtime_dir / "supplier_financial_source_migration_v1.json"
            ).read_text(encoding="utf-8")
        )
        if prepared_manifest.get("status") != "prepared":
            raise AssertionError("pre-change migration manifest was not durable")
        if not all((runtime_dir / path).is_file() for path in old_paths.values()):
            raise AssertionError("pre-database interruption removed legacy sources")
        applied = run(action="apply", runtime_dir=runtime_dir)
        if (
            applied.get("status") != "applied"
            or applied.get("group_count") != 1
            or applied.get("document_count") != 2
            or not (applied.get("readback") or {}).get("readback_confirmed")
        ):
            raise AssertionError(f"migration apply failed: {applied}")
        lifecycle = dict(applied.get("orphan_lifecycle") or {})
        if orphan.exists() or len(lifecycle.get("quarantined") or []) != 1:
            raise AssertionError(
                f"unreferenced old staging source was not quarantined: {lifecycle}"
            )
        quarantined_path = runtime_dir / str(
            lifecycle["quarantined"][0]["quarantine_path"]
        )
        if not quarantined_path.is_file():
            raise AssertionError("orphan quarantine readback is missing")
        target = (
            runtime_dir
            / "supplier_financial_sources"
            / "sha256"
            / source_sha256[:2]
            / source_sha256
            / "source.pdf"
        )
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != source_sha256:
            raise AssertionError("content-addressed source readback failed")
        for document_id, old_path in old_paths.items():
            if (runtime_dir / old_path).exists():
                raise AssertionError("legacy duplicate source path remains")
            stored = next(
                item
                for item in runtime.list_supplier_financial_documents_all()
                if item["document_id"] == document_id
            )
            if stored["stored_file_path"] != str(target.relative_to(runtime_dir)):
                raise AssertionError("document was not relinked to exact source")
        runtime.save_supplier_shipment(
            header={
                "shipment_id": "source-migration-reuse",
                "created_at": STAMP,
                "updated_at": STAMP,
                "shipment_date": "2026-07-26",
                "invoice_no": "MIGRATION-REUSE",
                "currency": "CNY",
            },
            lines=[],
        )

        def unexpected_reparse(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("same SHA was parsed again")

        reused = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: STAMP,
            pdf_text_extractor=unexpected_reparse,
        ).upload_bank_fee_statement_preview(
            "source-migration-reuse",
            file_bytes=body,
            uploaded_filename="statement-reused.pdf",
            uploaded_content_type="application/pdf",
        )
        if reused.get("source_sha256") != source_sha256:
            raise AssertionError("same-SHA source parse was not reused")
        repeated = run(action="apply", runtime_dir=runtime_dir)
        if repeated.get("status") != "already_applied" or not repeated.get(
            "idempotent"
        ):
            raise AssertionError("source migration repeat is not a no-op")

        rolled_back = run(action="rollback", runtime_dir=runtime_dir)
        if not rolled_back.get("readback_confirmed"):
            raise AssertionError(f"source migration rollback failed: {rolled_back}")
        inodes = set()
        for old_path in old_paths.values():
            restored = runtime_dir / old_path
            if not restored.is_file():
                raise AssertionError("rollback did not restore legacy source link")
            inodes.add(restored.stat().st_ino)
        inodes.add(target.stat().st_ino)
        if len(inodes) != 1:
            raise AssertionError("rollback duplicated bytes instead of restoring hardlinks")
    print("supplier_financial_source_migration_smoke: OK")


if __name__ == "__main__":
    main()
