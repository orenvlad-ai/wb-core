"""Smoke-check для delivery bundle wide data matrix v1."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wide_data_matrix_delivery_bundle_v1 import WideDataMatrixDeliveryBundleV1Block
from packages.contracts.wide_data_matrix_delivery_bundle_v1 import WideDataMatrixDeliveryBundleV1Request

ARTIFACTS = ROOT / "artifacts" / "wide_data_matrix_delivery_bundle_v1"


def _expected_fixture(name: str) -> dict:
    path = ARTIFACTS / "target" / f"{name}__template__delivery-bundle__fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, expected_data_rows: int, expected_status_rows: int) -> None:
    block = WideDataMatrixDeliveryBundleV1Block()
    result = asdict(block.execute(WideDataMatrixDeliveryBundleV1Request(bundle_type="wide_data_matrix_delivery_bundle_v1", scenario=name)))
    if result != _expected_fixture(name):
        raise AssertionError(f"{name}: result differs from target fixture")
    if "data_vitrina" not in result or "status" not in result:
        raise AssertionError(f"{name}: missing delivery sections")
    data_rows = len(result["data_vitrina"]["rows"])
    status_rows = len(result["status"]["rows"])
    if data_rows != expected_data_rows:
        raise AssertionError(f"{name}: expected data rows={expected_data_rows}, got {data_rows}")
    if status_rows != expected_status_rows:
        raise AssertionError(f"{name}: expected status rows={expected_status_rows}, got {status_rows}")
    print(f"{name}: ok -> success")
    print(f"{name}: data_vitrina rows -> {data_rows}")
    print(f"{name}: status rows -> {status_rows}")


def main() -> None:
    _check_case("normal", expected_data_rows=18, expected_status_rows=11)
    _check_case("minimal", expected_data_rows=0, expected_status_rows=1)
    print("smoke-check passed")


if __name__ == "__main__":
    main()
