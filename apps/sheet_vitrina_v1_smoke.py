"""Smoke-check для sheet vitrina v1 scaffold."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1 import SheetVitrinaV1Block
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Request

ARTIFACTS = ROOT / "artifacts" / "sheet_vitrina_v1"


def _expected_fixture() -> dict:
    path = ARTIFACTS / "target" / "normal__template__sheet-write-plan__fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    block = SheetVitrinaV1Block()
    result = asdict(block.execute(SheetVitrinaV1Request(bundle_type="sheet_vitrina_v1")))
    if result != _expected_fixture():
        raise AssertionError("normal: result differs from target fixture")
    sheets = result["sheets"]
    if len(sheets) != 2:
        raise AssertionError(f"expected 2 sheets, got {len(sheets)}")
    print("normal: ok -> success")
    print(f"normal: sheets -> {len(sheets)}")
    print(f"normal: data_vitrina rows -> {sheets[0]['row_count']}")
    print(f"normal: status rows -> {sheets[1]['row_count']}")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
