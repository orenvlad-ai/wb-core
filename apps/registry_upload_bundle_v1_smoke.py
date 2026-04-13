"""Smoke-check для registry upload bundle v1."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_bundle_v1 import RegistryUploadBundleV1Block


def main() -> None:
    block = RegistryUploadBundleV1Block()
    bundle = block.build_bundle()
    result = asdict(bundle)
    expected = block.load_target_fixture()
    if result != expected:
        raise AssertionError("result differs from target fixture")

    report = block.validate_bundle(bundle)
    print("bundle: ok -> input parity")
    print(f"bundle_version: ok -> {report.bundle_version}")
    print(f"config_v2: count -> {report.config_count}")
    print(f"metrics_v2: count -> {report.metric_count}")
    print(f"formulas_v2: count -> {report.formula_count}")
    print(f"scopes: ok -> {', '.join(report.scope_values)}")
    print(f"calc_types: ok -> {', '.join(report.calc_types)}")
    print(f"runtime_metric_keys_checked: count -> {report.runtime_metric_keys_checked}")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
