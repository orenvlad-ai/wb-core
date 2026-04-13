"""Smoke-check для DB-backed runtime registry upload."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
        print("smoke-check passed")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
