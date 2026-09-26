#!/usr/bin/env python3
"""Fixture proof for the reversible bank-statement source migration."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_financial_source_migration import run, build_plan, _run_orphan_lifecycle, _orphan_reference_readback, MANIFEST_FILENAME  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    SupplierFinancialDocumentsBlock,
)


STAMP = "2026-07-26T09:00:00Z"


def _reference_safety() -> None:
    from packages.application.storage_registry import StoreRegistry, atomic_write_manifest, build_manifest
    with TemporaryDirectory(prefix="source-unavailable-") as directory:
        root = Path(directory).resolve()
        result = run(action="apply", runtime_dir=root)
        assert result["status"] == "held_source_store_unavailable"
        assert result["orphan_lifecycle"]["status"] == "held_unknown_reference_coverage"
        assert not (root / "registry_upload_runtime.sqlite3").exists()
    with TemporaryDirectory(prefix="source-reference-safety-") as directory:
        root = Path(directory).resolve()
        selected = root / "operational.sqlite3"
        legacy = root / "registry_upload_runtime.sqlite3"
        def seed(path):
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE sheet_vitrina_v1_supplier_financial_documents
                        (document_id TEXT, document_type TEXT, stored_file_path TEXT, file_sha256 TEXT);
                    CREATE TABLE sheet_vitrina_v1_cny_documents (stored_file_path TEXT, file_sha256 TEXT);
                    CREATE TABLE sheet_vitrina_v1_supplier_financial_sources (stored_file_path TEXT, source_sha256 TEXT);
                    CREATE TABLE extra_document_owner (source_file_path TEXT, source_file_sha256 TEXT);
                """)
        seed(selected)
        seed(legacy)
        manifest = build_manifest(state="cutover", canonical_source="split", generation_epoch="epoch-1",
            raw_generation_id="raw-1", raw_relative_path="raw.sqlite3", raw_watermark="",
            operational_generation_id="op-1", operational_relative_path=selected.name,
            operational_watermark="", rollback_generation_id="legacy", source_fingerprint="source-1")
        atomic_write_manifest(StoreRegistry(root).manifest_path, manifest)
        with sqlite3.connect(selected) as conn:
            conn.execute("CREATE TABLE finance_operational_schema_meta (singleton INTEGER, schema_revision TEXT, logical_store TEXT, generation_id TEXT, generation_epoch TEXT, source_fingerprint TEXT)")
            conn.execute("INSERT INTO finance_operational_schema_meta VALUES (1,?,?,?,?,?)",
                ("operational_v1", "operational", "op-1", "epoch-1", "source-1"))
        candidates = {}
        for name in ("financial", "cny", "path_only", "sources", "extra", "manifest", "unknown"):
            path = root / "supplier_financial_orphan_quarantine" / name / "source.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture-" + name).encode())
            old = time.time() - 31 * 86400
            os.utime(path, (old, old))
            candidates[name] = (path, hashlib.sha256(path.read_bytes()).hexdigest())
        with sqlite3.connect(selected) as conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES (?,?,?,?)",
                ("financial", "invoice", "missing/new-name.pdf", candidates["financial"][1]))
            conn.execute("INSERT INTO sheet_vitrina_v1_cny_documents VALUES (?,?)", ("missing/cny.pdf", candidates["cny"][1]))
            conn.execute("INSERT INTO sheet_vitrina_v1_cny_documents VALUES (?,?)",
                ("supplier_financial_documents/files/path_only/source.pdf", None))
            conn.execute("INSERT INTO sheet_vitrina_v1_cny_documents VALUES (?,?)", (None, None))
            conn.execute("INSERT INTO sheet_vitrina_v1_supplier_financial_sources VALUES (?,?)", ("missing/source.pdf", candidates["sources"][1]))
            conn.execute("INSERT INTO extra_document_owner VALUES (?,?)", ("missing/extra.pdf", candidates["extra"][1]))
        (root / MANIFEST_FILENAME).write_text(json.dumps({"plan": {"groups": [{"source_sha256": candidates["manifest"][1]}]}}))
        # Empty legacy is deliberately different from the selected operational DB.
        before = {p: p.read_bytes() for p in [selected, legacy] + [v[0] for v in candidates.values()]}
        result = _run_orphan_lifecycle(root)
        assert result["status"] == "held_unknown_reference_coverage"
        assert not result["expired_deleted"] and not result["quarantined"]
        assert result["reference_database"] == str(selected)
        for name, (path, _digest) in candidates.items():
            item = next(row for row in result["held"] if row["path"] == str(path.relative_to(root)))
            assert item["reason"] == ("unknown_reference_coverage" if name in {"unknown", "extra"} else "referenced_source")
        assert "extra_document_owner" not in result["reference_tables"]
        assert len(result["reference_tables"]) == 3
        assert "other table readers" in result["coverage_gap"]
        with patch("apps.supplier_financial_source_migration.ORPHAN_HASH_READ_LIMIT_BYTES", 20):
            bounded = _run_orphan_lifecycle(root)
        assert 0 < bounded["hash_read_bytes"] <= 20
        assert bounded["skipped_hash_count"] > 0
        for item in bounded["held"]:
            if item["sha256"] is None:
                assert item["reason"] == "unknown_reference_coverage"
                assert item["hash_status"] == "skipped_budget_or_drift"
        with patch("apps.supplier_financial_source_migration.ORPHAN_HASH_READ_LIMIT_BYTES", 0):
            zero_budget = _run_orphan_lifecycle(root)
        assert zero_budget["hash_read_bytes"] == 0
        assert all(item["sha256"] is None and item["reason"] == "unknown_reference_coverage"
                   for item in zero_budget["held"])
        assert all(p.read_bytes() == data for p, data in before.items())
        assert _run_orphan_lifecycle(root)["expired_deleted"] == []
        # Canonical planning must see the selected store, not the empty legacy DB.
        bank = root / "statement.pdf"
        bank.write_bytes(b"bank statement")
        with sqlite3.connect(selected) as conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES (?,?,?,?)",
                ("bank", "bank_fee_statement", bank.name, hashlib.sha256(bank.read_bytes()).hexdigest()))
        assert build_plan(root)["groups"][0]["documents"][0]["document_id"] == "bank"
        # Drift and incomplete legacy schema cannot turn missing knowledge into orphanhood.
        with sqlite3.connect(selected) as conn:
            conn.execute("UPDATE finance_operational_schema_meta SET generation_id='wrong'")
        assert _run_orphan_lifecycle(root)["reference_error"]
        assert all(path.is_file() for path, _digest in candidates.values())
        with sqlite3.connect(selected) as conn:
            conn.execute("UPDATE finance_operational_schema_meta SET generation_id='op-1'")
            conn.execute("DROP TABLE sheet_vitrina_v1_cny_documents")
        incomplete = _run_orphan_lifecycle(root)
        assert "missing" in incomplete["reference_error"]
        assert not incomplete["expired_deleted"]
        selected.unlink()
        missing = _run_orphan_lifecycle(root)
        assert missing["reference_error"] and not selected.exists()
        assert all(path.is_file() for path, _digest in candidates.values())


def main() -> None:
    _reference_safety()
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
        if (not orphan.is_file() or lifecycle.get("status") != "held_unknown_reference_coverage"
                or lifecycle["quarantined"] or lifecycle["expired_deleted"]):
            raise AssertionError(f"unknown orphan coverage must hold files: {lifecycle}")
        if not any(item["reason"] == "referenced_source" for item in lifecycle["held"]):
            raise AssertionError("same-SHA staging source was not classified as referenced")
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
