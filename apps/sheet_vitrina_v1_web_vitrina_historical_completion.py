"""Bounded historical compare/materialize tooling for web-vitrina ready snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.web_vitrina_historical_ready_snapshot_import import (
    DEFAULT_DATE_FROM,
    DEFAULT_DATE_TO,
    DEFAULT_WORKBOOK_SHEET_NAME,
    compare_historical_artifact_against_runtime,
    extract_historical_artifact_from_workbook,
    load_historical_artifact,
    materialize_historical_ready_snapshots,
    save_historical_artifact,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare and one-off materialize historical web-vitrina ready snapshots."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract-workbook-window")
    extract_parser.add_argument("--xlsx-path", required=True)
    extract_parser.add_argument("--sheet-name", default=DEFAULT_WORKBOOK_SHEET_NAME)
    extract_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    extract_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    extract_parser.add_argument("--output", required=True)

    compare_parser = subparsers.add_parser("compare-runtime-window")
    compare_parser.add_argument("--runtime-dir", required=True)
    compare_parser.add_argument("--input", required=True)
    compare_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    compare_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)

    materialize_parser = subparsers.add_parser("materialize-ready-window")
    materialize_parser.add_argument("--runtime-dir", required=True)
    materialize_parser.add_argument("--input", required=True)
    materialize_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    materialize_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    materialize_parser.add_argument("--replace-existing", action="store_true")
    materialize_parser.add_argument(
        "--captured-at",
        default=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )

    args = parser.parse_args()
    if args.command == "extract-workbook-window":
        _run_extract(args)
        return
    if args.command == "compare-runtime-window":
        _run_compare(args)
        return
    if args.command == "materialize-ready-window":
        _run_materialize(args)
        return
    raise SystemExit(f"unsupported command: {args.command}")


def _run_extract(args: argparse.Namespace) -> None:
    artifact = extract_historical_artifact_from_workbook(
        workbook_path=Path(args.xlsx_path).expanduser().resolve(),
        sheet_name=args.sheet_name,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    output_path = Path(args.output).expanduser().resolve()
    save_historical_artifact(artifact=artifact, output_path=output_path)
    print(
        json.dumps(
            {
                "status": "success",
                "output": str(output_path),
                "date_from": artifact.date_from,
                "date_to": artifact.date_to,
                "date_count": len(artifact.dates),
                "row_count": len(artifact.rows),
                "sheet_name": artifact.sheet_name,
                "source_file": artifact.source_file,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _run_compare(args: argparse.Namespace) -> None:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir).expanduser().resolve())
    artifact = load_historical_artifact(artifact_path=Path(args.input).expanduser().resolve())
    print(
        json.dumps(
            compare_historical_artifact_against_runtime(
                runtime=runtime,
                artifact=artifact,
                date_from=args.date_from,
                date_to=args.date_to,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


def _run_materialize(args: argparse.Namespace) -> None:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir).expanduser().resolve())
    artifact = load_historical_artifact(artifact_path=Path(args.input).expanduser().resolve())
    print(
        json.dumps(
            materialize_historical_ready_snapshots(
                runtime=runtime,
                artifact=artifact,
                captured_at=str(args.captured_at),
                date_from=args.date_from,
                date_to=args.date_to,
                replace_existing=bool(args.replace_existing),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
