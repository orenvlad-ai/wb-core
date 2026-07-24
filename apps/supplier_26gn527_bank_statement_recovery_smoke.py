#!/usr/bin/env python3
"""Smoke-check the canonical file boundary used by the 26GN527 runner."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_26gn527_bank_statement_recovery import (  # noqa: E402
    _resolve_source_file_path,
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

    print("supplier_26gn527_bank_statement_recovery_smoke: OK")


if __name__ == "__main__":
    main()
