"""Smoke-check для wide data matrix v1 fixture."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wide_data_matrix_v1 import WideDataMatrixV1FixtureBlock
from packages.contracts.wide_data_matrix_v1 import WideDataMatrixV1Request

ARTIFACTS = ROOT / "artifacts" / "wide_data_matrix_v1"


def _expected_fixture(name: str) -> dict:
    path = ARTIFACTS / "target" / f"{name}__template__target__fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, request: WideDataMatrixV1Request, expected_kind: str, expected_rows: int) -> None:
    block = WideDataMatrixV1FixtureBlock()
    result = asdict(block.execute(request))
    if result != _expected_fixture(name):
        raise AssertionError(f"{name}: result differs from target fixture")
    kind = result["result"]["kind"]
    rows = len(result["result"]["rows"])
    if kind != expected_kind:
        raise AssertionError(f"{name}: expected kind={expected_kind}, got {kind}")
    if rows != expected_rows:
        raise AssertionError(f"{name}: expected rows={expected_rows}, got {rows}")
    print(f"{name}: ok -> {kind}")
    print(f"{name}: rows -> {rows}")


def main() -> None:
    _check_case(
        "normal",
        WideDataMatrixV1Request(bundle_type="wide_data_matrix_v1"),
        expected_kind="success",
        expected_rows=18,
    )
    _check_case(
        "minimal",
        WideDataMatrixV1Request(bundle_type="wide_data_matrix_v1", scenario="minimal"),
        expected_kind="empty",
        expected_rows=0,
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
