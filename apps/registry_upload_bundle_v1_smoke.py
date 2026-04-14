"""Smoke-check для registry upload bundle v1."""

from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.registry_upload_smoke_support import (
    LEGACY_CONFIG_CAP,
    LEGACY_FORMULAS_CAP,
    LEGACY_METRICS_CAP,
    build_synthetic_oversized_bundle,
    write_runtime_registry_fixture,
)
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

    synthetic_bundle = build_synthetic_oversized_bundle()
    if len(synthetic_bundle.config_v2) <= LEGACY_CONFIG_CAP:
        raise AssertionError("synthetic config_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.metrics_v2) <= LEGACY_METRICS_CAP:
        raise AssertionError("synthetic metrics_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.formulas_v2) <= LEGACY_FORMULAS_CAP:
        raise AssertionError("synthetic formulas_v2 count must exceed legacy hardcoded cap")

    with TemporaryDirectory(prefix="registry-upload-bundle-smoke-") as tmp:
        runtime_registry_path = Path(tmp) / "runtime_registry.json"
        write_runtime_registry_fixture(runtime_registry_path, synthetic_bundle)
        synthetic_block = RegistryUploadBundleV1Block(runtime_registry_path=runtime_registry_path)
        synthetic_report = synthetic_block.validate_bundle(
            synthetic_bundle,
            enforce_fixture_uniqueness=False,
        )

    if synthetic_report.config_count != len(synthetic_bundle.config_v2):
        raise AssertionError("validator must accept all synthetic config_v2 rows")
    if synthetic_report.metric_count != len(synthetic_bundle.metrics_v2):
        raise AssertionError("validator must accept all synthetic metrics_v2 rows")
    if synthetic_report.formula_count != len(synthetic_bundle.formulas_v2):
        raise AssertionError("validator must accept all synthetic formulas_v2 rows")
    if synthetic_report.runtime_metric_keys_checked != len(synthetic_bundle.metrics_v2):
        raise AssertionError("validator must check every synthetic runtime metric key")

    print(
        "uncapped synthetic bundle: ok -> "
        f"{synthetic_report.config_count}/{synthetic_report.metric_count}/{synthetic_report.formula_count}"
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
