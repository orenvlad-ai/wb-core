"""Archived local runner for the legacy bound Google Sheet contour."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1 import SheetVitrinaV1Block
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Request

TARGET_SPREADSHEET_ID = "1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE"
TARGET_SCRIPT_ID = "1QalhdgdmpxekaTMbNEZM1ubLSPKkTYZ53SHacqBU9HRVJQgEKRdHkgSf"
TARGET_SPREADSHEET_NAME = "WB Core Vitrina V1"
ARCHIVE_MESSAGE = (
    "Legacy Google Sheets contour is archived. "
    "This runner must not push or write sheet_vitrina_v1; use web-vitrina/operator instead."
)


def _run_command(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return (completed.stdout or "") + (completed.stderr or "")


def _parse_json_from_output(output: str) -> Any:
    stripped = output.strip()
    candidates = [stripped, *reversed([line.strip() for line in stripped.splitlines() if line.strip()])]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    json_match = re.search(r"(\{.*\}|\[.*\])", stripped, flags=re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))
    raise ValueError(f"unable to parse JSON from clasp output: {output}")


def _load_clasp_config() -> dict[str, Any]:
    return json.loads((ROOT / ".clasp.json").read_text(encoding="utf-8"))


def _verify_target_config() -> None:
    config = _load_clasp_config()
    if config.get("scriptId") != TARGET_SCRIPT_ID:
        raise ValueError("unexpected scriptId in .clasp.json")
    if config.get("parentId") != TARGET_SPREADSHEET_ID:
        raise ValueError("unexpected parentId in .clasp.json")
    if config.get("rootDir") != "gas/sheet_vitrina_v1":
        raise ValueError("unexpected rootDir in .clasp.json")


def _build_write_plan() -> dict[str, Any]:
    block = SheetVitrinaV1Block()
    return asdict(block.execute(SheetVitrinaV1Request(bundle_type="sheet_vitrina_v1")))


def main() -> None:
    raise SystemExit(ARCHIVE_MESSAGE)
    _verify_target_config()
    plan = _build_write_plan()
    _run_command(["clasp", "push"])

    target_info = _parse_json_from_output(_run_command(["clasp", "run", "getSheetVitrinaBridgeTargetInfo"]))
    if target_info["spreadsheet_id"] != TARGET_SPREADSHEET_ID:
        raise AssertionError("target spreadsheet id mismatch")
    if target_info["spreadsheet_name"] != TARGET_SPREADSHEET_NAME:
        raise AssertionError("target spreadsheet name mismatch")

    write_result = _parse_json_from_output(
        _run_command(
            [
                "clasp",
                "run",
                "writeSheetVitrinaV1Plan",
                "--params",
                json.dumps([json.dumps(plan, ensure_ascii=False, separators=(",", ":"))], ensure_ascii=False),
            ]
        )
    )
    if write_result["spreadsheet_id"] != TARGET_SPREADSHEET_ID:
        raise AssertionError("write result spreadsheet id mismatch")

    state = _parse_json_from_output(_run_command(["clasp", "run", "getSheetVitrinaV1State"]))
    sheets = {sheet["sheet_name"]: sheet for sheet in state["sheets"]}
    data_sheet = sheets["DATA_VITRINA"]
    status_sheet = sheets["STATUS"]

    if not data_sheet["present"] or data_sheet["last_row"] != 19 or data_sheet["last_column"] != 5:
        raise AssertionError(f"DATA_VITRINA unexpected state: {data_sheet}")
    if not status_sheet["present"] or status_sheet["last_row"] != 12 or status_sheet["last_column"] != 11:
        raise AssertionError(f"STATUS unexpected state: {status_sheet}")

    print(f"target: ok -> {target_info['spreadsheet_name']}")
    print(f"DATA_VITRINA: ok -> {write_result['sheets'][0]['write_rect']}")
    print(f"STATUS: ok -> {write_result['sheets'][1]['write_rect']}")
    print("live-write-check passed")


if __name__ == "__main__":
    main()
