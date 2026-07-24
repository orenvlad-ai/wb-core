#!/usr/bin/env python3
"""Smoke-check the canonical file boundary used by the 26GN527 runner."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_26gn527_bank_statement_recovery import (  # noqa: E402
    SHIPMENT_ID,
    _readonly_target_snapshot,
    _resolve_source_file_path,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


def main() -> None:
    with TemporaryDirectory(prefix="supplier-26gn527-source-boundary-") as temp:
        runtime_dir = Path(temp) / "runtime"
        source_path = runtime_dir / "supplier_financial_documents" / "source.pdf"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"immutable statement")

        relative = _resolve_source_file_path(
            runtime_dir,
            {"stored_file_path": "supplier_financial_documents/source.pdf"},
        )
        if relative != source_path.resolve():
            raise AssertionError(
                "relative source path did not resolve under canonical runtime"
            )

        absolute = _resolve_source_file_path(
            runtime_dir,
            {"stored_file_path": str(source_path.resolve())},
        )
        if absolute != source_path.resolve():
            raise AssertionError("absolute canonical source path changed")

        try:
            _resolve_source_file_path(
                runtime_dir,
                {"stored_file_path": "../outside.pdf"},
            )
        except ValueError as exc:
            if "escapes canonical runtime" not in str(exc):
                raise
        else:
            raise AssertionError("source path traversal must fail closed")

    with TemporaryDirectory(prefix="supplier-26gn527-bounded-snapshot-") as temp:
        root = Path(temp)
        source_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=root / "source"
        )
        _save_shipment(source_runtime, SHIPMENT_ID, "26GN527")
        _save_shipment(source_runtime, "unrelated-shipment", "OTHER")
        target_path = root / "target" / source_runtime.db_path.name
        _readonly_target_snapshot(source_runtime.db_path, target_path)
        with sqlite3.connect(
            f"file:{target_path.resolve()}?mode=ro", uri=True
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            shipment_ids = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT shipment_id
                    FROM sheet_vitrina_v1_supplier_shipments
                    ORDER BY shipment_id
                    """
                ).fetchall()
            ]
            integrity = str(
                conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
        if shipment_ids != [SHIPMENT_ID]:
            raise AssertionError(
                f"bounded snapshot leaked unrelated shipments: {shipment_ids}"
            )
        if integrity.lower() != "ok":
            raise AssertionError(
                f"bounded snapshot integrity check failed: {integrity}"
            )

    print("supplier_26gn527_bank_statement_recovery_smoke: OK")


def _save_shipment(
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
    invoice_no: str,
) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": shipment_id,
            "created_at": "2026-07-24T08:00:00Z",
            "updated_at": "2026-07-24T08:00:00Z",
            "shipment_date": "2026-06-25",
            "order_status": "production",
            "invoice_no": invoice_no,
            "invoice_date": "2026-06-25",
        },
        lines=[],
    )


if __name__ == "__main__":
    main()
