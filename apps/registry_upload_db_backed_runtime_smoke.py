"""Smoke-check для DB-backed runtime registry upload."""

from dataclasses import asdict
import json
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
from packages.application.registry_upload_db_backed_runtime import (
    INPUT_BUNDLE_FIXTURE,
    RegistryUploadDbBackedRuntime,
)

ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_db_backed_runtime"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:00:02Z"


def main() -> None:
    with TemporaryDirectory(prefix="registry-upload-db-backed-runtime-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")

        accepted_result = runtime.ingest_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        accepted_expected = _load_json(TARGET_DIR / "upload_result__accepted__fixture.json")
        if asdict(accepted_result) != accepted_expected:
            raise AssertionError("accepted runtime result differs from target fixture")

        persisted_result = runtime.load_persisted_upload_result(accepted_result.bundle_version)
        if asdict(persisted_result) != accepted_expected:
            raise AssertionError("persisted runtime result differs from target fixture")

        current_state = runtime.load_current_state()
        current_expected = _load_json(TARGET_DIR / "current_state__fixture.json")
        if asdict(current_state) != current_expected:
            raise AssertionError("runtime current state differs from target fixture")

        version_index_expected = _load_json(TARGET_DIR / "version_index__fixture.json")
        if runtime.list_bundle_versions() != version_index_expected:
            raise AssertionError("runtime version index differs from target fixture")

        duplicate_result = runtime.ingest_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        duplicate_expected = _load_json(TARGET_DIR / "upload_result__duplicate_bundle_version__fixture.json")
        if asdict(duplicate_result) != duplicate_expected:
            raise AssertionError("duplicate runtime result differs from target fixture")

        if asdict(runtime.load_current_state()) != current_expected:
            raise AssertionError("runtime current state changed after duplicate rejection")
        if runtime.list_bundle_versions() != version_index_expected:
            raise AssertionError("runtime version index changed after duplicate rejection")

        print(f"accepted status: ok -> {accepted_result.status}")
        print(f"db file: ok -> {runtime.db_path.name}")
        print(f"current bundle_version: ok -> {current_state.bundle_version}")
        print(f"duplicate status: ok -> {duplicate_result.status}")

    synthetic_bundle = build_synthetic_oversized_bundle()
    if len(synthetic_bundle.config_v2) <= LEGACY_CONFIG_CAP:
        raise AssertionError("synthetic config_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.metrics_v2) <= LEGACY_METRICS_CAP:
        raise AssertionError("synthetic metrics_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.formulas_v2) <= LEGACY_FORMULAS_CAP:
        raise AssertionError("synthetic formulas_v2 count must exceed legacy hardcoded cap")

    with TemporaryDirectory(prefix="registry-upload-db-backed-runtime-uncapped-") as tmp:
        runtime_registry_path = Path(tmp) / "runtime_registry.json"
        write_runtime_registry_fixture(runtime_registry_path, synthetic_bundle)
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime",
            bundle_block=RegistryUploadBundleV1Block(runtime_registry_path=runtime_registry_path),
        )
        accepted_result = runtime.ingest_bundle(synthetic_bundle, activated_at=ACTIVATED_AT)
        if accepted_result.status != "accepted":
            raise AssertionError("synthetic oversized bundle must be accepted by DB-backed runtime")
        if accepted_result.accepted_counts.config_v2 != len(synthetic_bundle.config_v2):
            raise AssertionError("runtime must persist all synthetic config_v2 rows")
        if accepted_result.accepted_counts.metrics_v2 != len(synthetic_bundle.metrics_v2):
            raise AssertionError("runtime must persist all synthetic metrics_v2 rows")
        if accepted_result.accepted_counts.formulas_v2 != len(synthetic_bundle.formulas_v2):
            raise AssertionError("runtime must persist all synthetic formulas_v2 rows")

        current_state = runtime.load_current_state()
        if len(current_state.config_v2) != len(synthetic_bundle.config_v2):
            raise AssertionError("runtime current_state must keep all synthetic config_v2 rows")
        if len(current_state.metrics_v2) != len(synthetic_bundle.metrics_v2):
            raise AssertionError("runtime current_state must keep all synthetic metrics_v2 rows")
        if len(current_state.formulas_v2) != len(synthetic_bundle.formulas_v2):
            raise AssertionError("runtime current_state must keep all synthetic formulas_v2 rows")

    print(
        "uncapped runtime bundle: ok -> "
        f"{len(synthetic_bundle.config_v2)}/{len(synthetic_bundle.metrics_v2)}/{len(synthetic_bundle.formulas_v2)}"
    )
    print("smoke-check passed")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
